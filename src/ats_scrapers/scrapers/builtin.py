"""Built In (https://builtin.com): US tech jobs scraper.

Built In is a US-focused tech-jobs board. Each listing page embeds a
schema.org ``ItemList`` with its 10 to 25 jobs (title, URL and a short
description), which we parse without any JS rendering.

The listing stops at page 1,000, newest first, so the unfiltered
``/jobs`` walk only reaches the newest ~12k of a far larger US
catalogue. The scraper also walks the board's own filter views, one per
remote preference, category and experience level; a view still full at
its last page is split by company size. The filter options come from
the listing's ``Builtin.jobBoardInit`` state. Deep pages of a large view
drop jobs that its narrower views list, so the narrow views read more
than their parent.

Requests go through ``httpx`` first and switch to ``httpcloak`` (TLS+h2
impersonation, in the ``scrapers`` extra) on the first 403.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import itertools
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_ROOT = "https://builtin.com"
LISTING_PATH = "/jobs"
DEFAULT_MAX_PAGES = 1000  # The listing serves no page past 1,000.
# A filter view without a country falls back to the country of the caller's IP.
COUNTRY = "USA"
# One address reads about 0.7 pages a second under Cloudflare's limit: the
# whole catalogue takes most of a day on a single route.
DEFAULT_MAX_SECONDS = 6 * 3600
# Reads of one page, each by whichever route is free, before its view stops.
PAGE_ATTEMPTS = 3
# Cloudflare answers a burst of 25 to 50 pages with a 429 challenge that
# lifts after about 15 seconds. Both request paths wait it out (5, 10, 20,
# 40, 60 s) instead of ending the walk early.
MAX_RETRIES = 6
RETRY_BASE_DELAY = 5.0
MAX_RETRY_DELAY = 60.0

# Built In serves the JSON-LD with `&#x2B;` instead of '+' in the type
# attribute. Match either; one regex per page payload.
_LD_RE = re.compile(
    r'<script[^>]+type="application/ld(?:\+|&#x2B;)json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_URL_ID_RE = re.compile(r"^https?://[^/]+/job/[^/]+/(?P<id>\d+)/?$")
_BOARD_INIT = "Builtin.jobBoardInit("
# View path segments in the board's own order: remote preference, category,
# experience level, then company size for a view still capped.
_VIEW_FACETS = (
    "remotePreferenceFilterOptions",
    "categoriesFilterOptions",
    "experiencesFilterOptions",
)
_SPLIT_FACET = "companySizesFilterOptions"


@ScraperRegistry.register(ATSType.BUILTIN)
class BuiltInScraper(BaseScraper):
    """Built In (builtin.com): US tech jobs.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages``: pagination cap of each view (default 1,000, the
      last page the listing serves; 10 to 25 jobs per page).
    - ``proxies``: routes the views are spread over, one reader each;
      ``None`` is a direct connection, even with a proxy in the
      environment. Defaults to ``[proxy]``.
    - ``max_seconds``: time budget. Past it the views left unread are
      dropped with a warning. ``None`` reads every view.
    """

    ats = ATSType.BUILTIN

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,*/*",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        proxies: Sequence[str | None] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_seconds: float | None = DEFAULT_MAX_SECONDS,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.max_pages = max_pages
        self.max_seconds = max_seconds
        # ``""`` is the fetcher's direct connection: a ``None`` route given
        # in ``proxies`` must not fall back to the environment's proxy.
        self.routes: list[str | None] = (
            [route or "" for route in proxies] if proxies else [proxy]
        )
        # Flipped to True the first time the direct ``httpx`` path
        # returns 403; subsequent requests in this scraper instance
        # then go through ``httpcloak``. Reset by re-instantiating.
        self._use_httpcloak = False

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}

        def absorb(items: list[Job]) -> None:
            for job in items:
                jobs.setdefault(job.ats_id, job)

        async with contextlib.AsyncExitStack() as stack:
            fetchers = [
                await stack.enter_async_context(self.make_fetcher(
                    proxy=route,
                    retries=MAX_RETRIES,
                    retry_base_delay=RETRY_BASE_DELAY,
                    max_retry_delay=MAX_RETRY_DELAY,
                ))
                for route in self.routes
            ]
            first = await self._request_html(
                fetchers[0], self.routes[0], _listing_url(LISTING_PATH, 1)
            )
            absorb(self._parse_listing(first))
            seeds = [(LISTING_PATH, 2)] if self.max_pages > 1 else []
            views: set[str] = set()
            sizes: list[str] = []
            facets = _board_facets(first)
            if facets is None:
                log.warning(
                    "Built In: no filter options on the listing; reading the "
                    "unfiltered listing only, which stops at page %d.",
                    self.max_pages,
                )
            else:
                levels, sizes = facets
                views = {
                    LISTING_PATH + "/" + "/".join(combo)
                    for combo in itertools.product(*levels)
                }
                seeds += [(view, 1) for view in sorted(views)]
            await self._walk(fetchers, seeds, views, sizes, absorb)
        return list(jobs.values())

    async def _walk(
        self,
        fetchers: list[Fetcher],
        seeds: list[tuple[str, int]],
        splittable: set[str],
        sizes: list[str],
        absorb: Callable[[list[Job]], None],
    ) -> None:
        """Read every view to its first empty page, one reader per route.

        The lowest page number goes first, so a run cut by the time budget
        still holds the newest jobs of every view.
        """
        queue: asyncio.PriorityQueue[tuple[int, int, str, int]] = asyncio.PriorityQueue()
        order = itertools.count()
        for path, page in seeds:
            queue.put_nowait((page, next(order), path, 1))
        deadline = (
            None if self.max_seconds is None
            else time.monotonic() + self.max_seconds
        )
        lost: list[str] = []
        capped: list[str] = []
        unread: set[str] = set()

        async def reader(fetcher: Fetcher, route: str | None) -> None:
            while True:
                page, _, path, attempt = await queue.get()
                try:
                    if deadline is not None and time.monotonic() > deadline:
                        unread.add(path)
                        continue
                    try:
                        text = await self._request_html(
                            fetcher, route, _listing_url(path, page)
                        )
                    except ScraperError as exc:
                        if attempt < PAGE_ATTEMPTS:
                            queue.put_nowait((page, next(order), path, attempt + 1))
                        else:
                            lost.append(f"{path} page {page} ({exc})")
                        continue
                    items = self._parse_listing(text)
                    absorb(items)
                    if not items:
                        continue
                    if page < self.max_pages:
                        queue.put_nowait((page + 1, next(order), path, 1))
                    elif path in splittable and sizes:
                        for size in sizes:
                            queue.put_nowait((1, next(order), f"{path}/{size}", 1))
                    elif path != LISTING_PATH:
                        capped.append(path)
                finally:
                    queue.task_done()

        readers = [
            asyncio.create_task(reader(fetcher, route))
            for fetcher, route in zip(fetchers, self.routes, strict=True)
        ]
        drained = asyncio.create_task(queue.join())
        try:
            done, _ = await asyncio.wait(
                [drained, *readers], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not drained:
                    task.result()  # A reader only stops by raising.
        finally:
            for task in (drained, *readers):
                task.cancel()
            await asyncio.gather(drained, *readers, return_exceptions=True)

        if lost:
            log.warning(
                "Built In: %d views stopped on a page that failed %d times, "
                "e.g. %s", len(lost), PAGE_ATTEMPTS, "; ".join(lost[:3]),
            )
        if capped:
            log.warning(
                "Built In: %d views still list jobs on page %d, the last one "
                "served: %s", len(capped), self.max_pages, ", ".join(capped[:5]),
            )
        if unread:
            log.warning(
                "Built In: the %ss time budget ran out with %d views unfinished.",
                self.max_seconds, len(unread),
            )

    # --- listing pages ------------------------------------------------------

    def _parse_listing(self, text: str) -> list[Job]:
        # The page embeds a single JSON-LD block whose ``@graph`` array
        # contains a CollectionPage + an ItemList. The ItemList's
        # ``itemListElement`` is the per-page job array.
        for match in _LD_RE.finditer(text):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            graph = (
                payload.get("@graph", [payload])
                if isinstance(payload, dict) else payload
            )
            if not isinstance(graph, list):
                graph = [graph]
            for node in graph:
                if not isinstance(node, dict):
                    continue
                if node.get("@type") == "ItemList":
                    items = node.get("itemListElement") or []
                    return [j for j in (self._parse_item(it) for it in items) if j]
        return []

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        if not isinstance(item, dict):
            return None
        url = (item.get("url") or "").strip()
        title = (item.get("name") or "").strip()
        if not url or not title:
            return None
        match = _JOB_URL_ID_RE.match(url)
        if not match:
            return None
        ats_id = match.group("id")
        description = item.get("description") or None
        if isinstance(description, str):
            description = _html_unescape_for_desc(description) or None

        return Job(
            url=url,
            title=title,
            company="Unknown",
            ats_type=ATSType.BUILTIN,
            ats_id=ats_id,
            description=description,
            fetched_at=datetime.now(UTC),
        )

    async def _request_html(
        self,
        fetcher: Fetcher,
        route: str | None,
        url: str,
    ) -> str:
        # Once a 403 has flipped the instance to httpcloak mode, every
        # subsequent request skips the wasted direct attempt.
        if self._use_httpcloak:
            return await self._request_via_httpcloak(url, route)

        # The shared Fetcher owns retries/backoff and 429/5xx handling.
        # 403 is a Cloudflare block signal, not an error: handle it
        # here so we can flip to the httpcloak fallback.
        response = await fetcher.request("GET", url, handled={403})
        if response.status_code == 403:
            log.info(
                "Built In: 403 on %s, switching to httpcloak fallback", url,
            )
            self._use_httpcloak = True
            return await self._request_via_httpcloak(url, route)
        return response.text

    async def _request_via_httpcloak(self, url: str, route: str | None) -> str:
        """TLS+h2 impersonation fallback used when builtin.com 403's
        the direct httpx user-agent. Verified live 2026-05-09: 200 with
        full ~390 KB HTML where direct returns 403/919 B.

        Cloudflare also rate-limits deep pagination via httpcloak: a
        403 or 429 here is treated as transient (retry with backoff) and
        only escalates to a hard ``ScraperError`` if it survives every
        retry.
        """
        from importlib.util import find_spec

        if find_spec("httpcloak") is None:
            raise ScraperError(
                "Built In's 403 fallback needs httpcloak: "
                "`pip install ats-scrapers[scrapers]`."
            )

        last_status: int | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            text = await asyncio.to_thread(self._httpcloak_get_sync, url, route)
            if isinstance(text, str):
                return text
            # ``_httpcloak_get_sync`` returned the int status on non-200
            # so we can decide here whether to retry or escalate.
            last_status = text
            if last_status not in (403, 429) or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(
                min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
            )
        raise ScraperError(
            f"Built In httpcloak fallback returned {last_status} for "
            f"{url} after {MAX_RETRIES} retries"
        )

    def _httpcloak_get_sync(self, url: str, route: str | None) -> str | int:
        """Sync fetch via httpcloak. Returns the page text on 200, the
        bare status int otherwise so the async caller can decide
        retry/escalate without raising for transient blocks."""
        import httpcloak

        kwargs: dict[str, str] = {}
        if route:
            kwargs["proxy"] = route
        r = httpcloak.get(url, timeout=self.timeout, **kwargs)
        if r.status_code != 200:
            return int(r.status_code)
        content = r.content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return content


def _listing_url(path: str, page: int) -> str:
    """``/jobs`` defaults to the US; a filter view needs the country."""
    if path == LISTING_PATH:
        return f"{API_ROOT}{path}?page={page}"
    return f"{API_ROOT}{path}?country={COUNTRY}&page={page}"


def _board_facets(text: str) -> tuple[list[list[str]], list[str]] | None:
    """The view facets' aliases, and the company sizes, from the
    ``Builtin.jobBoardInit`` state; ``None`` when one is missing."""
    start = text.find(_BOARD_INIT)
    if start == -1:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(text, start + len(_BOARD_INIT))
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    levels: list[list[str]] = []
    for key in (*_VIEW_FACETS, _SPLIT_FACET):
        options = state.get(key)
        aliases = [
            option["alias"]
            for option in (options if isinstance(options, list) else [])
            if isinstance(option, dict)
            and isinstance(option.get("alias"), str)
            and option["alias"]
        ]
        if not aliases:
            return None
        levels.append(aliases)
    return levels[:-1], levels[-1]


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure.
    Replaces the legacy _strip_html/_html_to_text path for descriptions
    only — title/company/salary fields still use the strip variant."""
    import html as _h
    if not isinstance(value, str):
        return None
    out = _h.unescape(value).strip()
    if not out:
        return None
    return out[:cap]


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
