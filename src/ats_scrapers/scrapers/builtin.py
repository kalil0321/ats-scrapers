"""Built In (https://builtin.com): US tech jobs scraper.

Built In is a US-focused tech-jobs board. Each listing page embeds a
schema.org ``ItemList`` with its 10 to 25 jobs (title and URL) and a card
per job with its company, places, attendance and pay, which we parse
without any JS rendering. The ``ItemList`` description is a summary Built
In writes, not the posting: the posting is the ``JobPosting`` JSON-LD of
the job's own page, read by :meth:`BuiltInScraper.get_description` (and
for every job when ``include_descriptions`` is set), along with its
posting date, structured places and pay.

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
import queue
import re
import time
from datetime import UTC, datetime
from importlib.util import find_spec
from typing import TYPE_CHECKING, ClassVar

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import proxy_url_from_env
from ats_scrapers.models import ATSType, Job, normalize_compensation_period
from ats_scrapers.scrapers._job_places import (
    COUNTRY_NAMES,
    ISO3_TO_ISO2,
    distinct_places,
    job_location,
)
from ats_scrapers.scrapers._posting_detail import job_posting
from ats_scrapers.scrapers._posting_fields import finite_number
from ats_scrapers.scrapers._source_timestamp import source_timestamp
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import Any

    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.models import EmploymentType, JobLocation, WorkplaceType

API_ROOT = "https://builtin.com"
LISTING_PATH = "/jobs"
DEFAULT_MAX_PAGES = 1000  # The listing serves no page past 1,000.
# A filter view without a country falls back to the country of the caller's IP.
COUNTRY = "USA"
# One address reads about 0.7 pages a second under Cloudflare's limit: the
# whole catalogue takes most of a day on a single route.
DEFAULT_MAX_SECONDS = 6 * 3600
# Job pages a route starts each second, one at a time.
DETAIL_RATE = 0.7
# Reads of one page, each by whichever route is free, before its view stops.
PAGE_ATTEMPTS = 3
# Cloudflare answers a burst of 25 to 50 pages with a 429 challenge that
# lifts after about 15 seconds. Both request paths wait it out (5, 10, 20,
# 40, 60 s) instead of ending the walk early.
MAX_RETRIES = 6
RETRY_BASE_DELAY = 5.0
MAX_RETRY_DELAY = 60.0
UNKNOWN_COMPANY = "Unknown"

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

_CARD_RE = re.compile(r'<div id="job-card-(\d+)"')
_CARD_COMPANY_RE = re.compile(r'data-id="company-title"[^>]*>\s*<span>([^<]*)</span>')
# A card value follows its Font Awesome icon: in a span of its own, or, for
# the posting age, in the icon's span.
_CARD_VALUE_RE = re.compile(
    r'<i class="fa-[a-z]+ fa-(?P<icon>house-building|location-dot|sack-dollar)[ "][^>]*></i>'
    r'\s*</div>\s*(?:<div>)?\s*<span(?P<attrs>[^>]*)>(?P<text>[^<]*)'
)
# A card with several places lists them in its tooltip.
_CARD_PLACES_RE = re.compile(r'data-bs-title="([^"]*)"')
_TOOLTIP_PLACE_RE = re.compile(r">([^<]+)</div>")
_PLACE_COUNT_RE = re.compile(r"\d+ Locations", re.IGNORECASE)
_ATTENDANCE: dict[str, WorkplaceType] = {
    "in-office": "onsite", "hybrid": "hybrid", "remote": "remote",
}
_EMPLOYMENT: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME", "PART_TIME": "PART_TIME", "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT", "INTERN": "INTERN", "INTERNSHIP": "INTERN",
    "TEMPORARY": "TEMPORARY",
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@ScraperRegistry.register(ATSType.BUILTIN)
class BuiltInScraper(BaseScraper):
    """Built In (builtin.com): US tech jobs.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages``: pagination cap of each view (default 1,000, the
      last page the listing serves; 10 to 25 jobs per page).
    - ``proxies``: routes the views and job pages are spread over, one
      reader each; ``None`` is a direct connection, even with a proxy in
      the environment. Defaults to ``[proxy]``.
    - ``max_seconds``: time budget of the listing and job pages. Past it
      the views left unread are dropped, and the jobs left unread keep
      no description, with a warning. ``None`` reads everything.
    - ``include_descriptions``: read every job's page after the listing.
      Off, :meth:`get_description` reads one job's page on demand.
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
        self._detail_routes = _RoutePool(self.routes, 1 / DETAIL_RATE)
        # Flipped to True the first time the direct ``httpx`` path
        # returns 403; subsequent requests in this scraper instance
        # then go through ``httpcloak``. Reset by re-instantiating.
        self._use_httpcloak = False

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        deadline = (
            None if self.max_seconds is None
            else time.monotonic() + self.max_seconds
        )

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
            await self._walk(fetchers, seeds, views, sizes, absorb, deadline)
        if self.include_descriptions:
            await self._read_postings(list(jobs.values()), deadline)
        return list(jobs.values())

    async def _walk(
        self,
        fetchers: list[Fetcher],
        seeds: list[tuple[str, int]],
        splittable: set[str],
        sizes: list[str],
        absorb: Callable[[list[Job]], None],
        deadline: float | None,
    ) -> None:
        """Read every view to its first empty page, one reader per route.

        The lowest page number goes first, so a run cut by the time budget
        still holds the newest jobs of every view.
        """
        queue_: asyncio.PriorityQueue[tuple[int, int, str, int]] = asyncio.PriorityQueue()
        order = itertools.count()
        for path, page in seeds:
            queue_.put_nowait((page, next(order), path, 1))
        lost: list[str] = []
        capped: list[str] = []
        unread: set[str] = set()

        async def reader(fetcher: Fetcher, route: str | None) -> None:
            while True:
                page, _, path, attempt = await queue_.get()
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
                            queue_.put_nowait((page, next(order), path, attempt + 1))
                        else:
                            lost.append(f"{path} page {page} ({exc})")
                        continue
                    items = self._parse_listing(text)
                    absorb(items)
                    if not items:
                        continue
                    if page < self.max_pages:
                        queue_.put_nowait((page + 1, next(order), path, 1))
                    elif path in splittable and sizes:
                        for size in sizes:
                            queue_.put_nowait((1, next(order), f"{path}/{size}", 1))
                    elif path != LISTING_PATH:
                        capped.append(path)
                finally:
                    queue_.task_done()

        readers = [
            asyncio.create_task(reader(fetcher, route))
            for fetcher, route in zip(fetchers, self.routes, strict=True)
        ]
        drained = asyncio.create_task(queue_.join())
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

    async def _read_postings(self, jobs: list[Job], deadline: float | None) -> None:
        """Every job's page, one reader per route, until the time budget ends."""
        waiting = iter(jobs)
        failed: list[str] = []
        unread = 0

        async def reader() -> None:
            nonlocal unread
            for job in waiting:
                if deadline is not None and time.monotonic() > deadline:
                    unread += 1
                    continue
                try:
                    await asyncio.to_thread(self.get_description, job)
                except ScraperError as exc:
                    failed.append(str(exc))

        await asyncio.gather(*(reader() for _ in self.routes))
        if failed:
            log.warning(
                "Built In: %d job pages failed, e.g. %s", len(failed), "; ".join(failed[:3]),
            )
        if unread:
            log.warning(
                "Built In: the %ss time budget ran out with %d job pages unread.",
                self.max_seconds, unread,
            )

    # --- job pages ----------------------------------------------------------

    def get_description(self, job: Job) -> str | None:
        """The posting from the job's own page; also sets the posting date,
        deadline, employment type, pay and structured places it gives."""
        if job.description:
            return job.description
        with self._detail_routes.take() as route:
            page = self._job_page(str(job.url), route)
        if page is None:
            return None
        # The shared reader expects the type attribute unencoded.
        posting = job_posting(page.replace("ld&#x2B;json", "ld+json"))
        if not posting:
            return None
        _apply_posting(job, posting)
        return job.description

    def _job_page(self, url: str, route: str | None) -> str | None:
        """The job's page, or ``None`` once Built In no longer serves it."""
        status = 0
        for attempt in range(1, MAX_RETRIES + 1):
            status, text = self._get_sync(url, route)
            if status == 200:
                return text
            if status in (404, 410):
                return None
            if status not in (0, 403, 429) and status < 500:
                break
            if attempt < MAX_RETRIES:
                time.sleep(min(RETRY_BASE_DELAY * 2 ** (attempt - 1), MAX_RETRY_DELAY))
        raise ScraperError(
            f"Built In answered {status or 'no response'} for {url} after "
            f"{MAX_RETRIES} attempts"
        )

    def _get_sync(self, url: str, route: str | None) -> tuple[int, str]:
        """Status and text of one GET; status 0 when no answer came."""
        if not self._use_httpcloak:
            proxy = route if route is not None else proxy_url_from_env()
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    proxy=proxy or None,
                    trust_env=proxy != "",
                    headers=self.default_headers,
                    follow_redirects=True,
                ) as client:
                    response = client.get(url)
            except httpx.HTTPError:
                return 0, ""
            if response.status_code != 403 or find_spec("httpcloak") is None:
                return response.status_code, response.text
            log.info("Built In: 403 on %s, switching to httpcloak fallback", url)
            self._use_httpcloak = True
        try:
            result = self._httpcloak_get_sync(url, route)
        except Exception:  # httpcloak reports every network failure this way
            return 0, ""
        return (200, result) if isinstance(result, str) else (result, "")

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
                    cards = _cards(text)
                    return [j for j in (self._parse_item(it, cards) for it in items) if j]
        return []

    def _parse_item(self, item: dict[str, Any], cards: dict[str, str]) -> Job | None:
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
        job = Job(
            url=url,
            title=title,
            company=UNKNOWN_COMPANY,
            ats_type=ATSType.BUILTIN,
            ats_id=ats_id,
            fetched_at=datetime.now(UTC),
        )
        if ats_id in cards:
            _apply_card(job, cards[ats_id])
        return job

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


class _RoutePool:
    """Lends each route to one request at a time, started at least
    ``interval`` seconds after the route's previous one."""

    def __init__(self, routes: Sequence[str | None], interval: float) -> None:
        self._interval = interval
        # (when the route may start again, tie-break, route)
        self._free: queue.PriorityQueue[tuple[float, int, str | None]] = queue.PriorityQueue()
        for index, route in enumerate(routes):
            self._free.put((0.0, index, route))

    @contextlib.contextmanager
    def take(self) -> Iterator[str | None]:
        ready_at, index, route = self._free.get()
        delay = ready_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        started = time.monotonic()
        try:
            yield route
        finally:
            self._free.put((started + self._interval, index, route))


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


# --- cards and postings -------------------------------------------------------


def _cards(text: str) -> dict[str, str]:
    """Each job card's markup on a listing page, by job id."""
    starts = list(_CARD_RE.finditer(text))
    ends = [match.start() for match in starts[1:]] + [len(text)]
    return {
        match.group(1): text[match.start():end]
        for match, end in zip(starts, ends, strict=False)
    }


def _apply_card(job: Job, card: str) -> None:
    company = _CARD_COMPANY_RE.search(card)
    if company and (name := html.unescape(company.group(1)).strip()):
        job.company = name
    values: dict[str, tuple[str, str]] = {}
    for match in _CARD_VALUE_RE.finditer(card):
        # The pay shows twice, for wide and narrow screens.
        values.setdefault(match["icon"], (match["attrs"], match["text"]))
    if "house-building" in values:
        job.workplace_type, job.is_remote = _attendance(html.unescape(values["house-building"][1]))
    if "sack-dollar" in values:
        job.salary_summary = html.unescape(values["sack-dollar"][1]).strip() or None
    if "location-dot" in values:
        attrs, text = values["location-dot"]
        tooltip = _CARD_PLACES_RE.search(attrs)
        labels = (
            _TOOLTIP_PLACE_RE.findall(html.unescape(tooltip.group(1))) if tooltip
            else [text]
        )
        labels = [
            label for label in (html.unescape(x).strip() for x in labels)
            if label and not _PLACE_COUNT_RE.fullmatch(label)
        ]
        if labels:
            job.location = "; ".join(labels)
            _set_places(job, [_card_place(label) for label in labels])


def _attendance(label: str) -> tuple[WorkplaceType | None, bool | None]:
    """``In-Office``, ``Hybrid``, ``Remote``, or two of them joined by ``or``."""
    modes = {_ATTENDANCE.get(part.strip().lower()) for part in label.split(" or ")}
    if None in modes:
        return None, None
    if len(modes) == 1:
        workplace = next(iter(modes))
    else:
        workplace = "onsite_or_remote" if modes == {"onsite", "remote"} else None
    remote = True if "remote" in modes else False if modes == {"onsite"} else None
    return workplace, remote


def _country_iso(name: object) -> str | None:
    """A country written as Built In writes it: ``USA``, ``DEU``, ``UK`` or ``Germany``."""
    if not isinstance(name, str):
        return None
    code = name.strip().upper()
    if code == "UK":
        return "GB"
    return ISO3_TO_ISO2.get(code) or COUNTRY_NAMES.get(name.strip().casefold())


def _card_place(label: str) -> JobLocation | None:
    """A card place: ``Boise, ID, USA``, ``Zürich, CHE`` or ``Germany``."""
    return job_location(label, country_code=_country_iso(label.rsplit(",", 1)[-1]))


def _set_places(job: Job, places: list[JobLocation | None]) -> None:
    kept = distinct_places(places)
    if not kept:
        return
    job.locations = kept
    countries = {place.country_iso for place in kept}
    if len(countries) == 1 and None not in countries:
        job.country_iso = countries.pop()


def _apply_posting(job: Job, posting: dict[str, Any]) -> None:
    """The fields a job page's ``JobPosting`` adds to its listing card."""
    description = _html_unescape_for_desc(posting.get("description"))
    if description:
        job.description = description
    organization = posting.get("hiringOrganization")
    if job.company == UNKNOWN_COMPANY and isinstance(organization, dict):
        name = organization.get("name")
        if isinstance(name, str) and name.strip():
            job.company = name.strip()
    job.posted_at = _posting_time(posting.get("datePosted")) or job.posted_at
    job.application_deadline = (
        source_timestamp(posting.get("validThrough")) or job.application_deadline
    )
    kinds = posting.get("employmentType")
    for kind in kinds if isinstance(kinds, list) else [kinds]:
        if isinstance(kind, str) and kind.upper() in _EMPLOYMENT:
            job.employment_type = _EMPLOYMENT[kind.upper()]
            break
    _apply_salary(job, posting.get("baseSalary"))
    places = posting.get("jobLocation")
    places = [p for p in (places if isinstance(places, list) else [places]) if isinstance(p, dict)]
    _set_places(job, [_posting_place(place) for place in places])
    if job.location is None and job.locations:
        job.location = "; ".join(place.label for place in job.locations)
    if len(places) == 1 and isinstance(geo := places[0].get("geo"), dict):
        lat, lon = finite_number(geo.get("latitude")), finite_number(geo.get("longitude"))
        if lat is not None and lon is not None:
            job.lat, job.lon = lat, lon
    if job.is_remote is None and posting.get("jobLocationType") == "TELECOMMUTE":
        job.is_remote = True


def _posting_place(place: dict[str, Any]) -> JobLocation | None:
    address = place.get("address")
    if not isinstance(address, dict):
        return None
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    return job_location(
        city=address.get("addressLocality"),
        region=address.get("addressRegion"),
        country=country,
        country_code=_country_iso(country),
    )


def _apply_salary(job: Job, salary: object) -> None:
    if not isinstance(salary, dict):
        return
    amounts = salary.get("value")
    if not isinstance(amounts, dict):
        amounts = {"value": amounts}
    low = finite_number(amounts.get("minValue"))
    if low is None:
        low = finite_number(amounts.get("value"))
    high = finite_number(amounts.get("maxValue"))
    currency = salary.get("currency")
    if low is None or not (isinstance(currency, str) and len(currency.strip()) == 3):
        return
    job.salary_currency = currency.strip().upper()
    job.salary_min = low
    job.salary_max = high if high is not None else low
    job.salary_period = normalize_compensation_period(amounts.get("unitText"))


def _posting_time(value: object) -> datetime | None:
    """``datePosted``: a day, or an instant with its offset."""
    if isinstance(value, str) and _DATE_RE.fullmatch(value.strip()):
        return datetime.fromisoformat(value.strip()).replace(tzinfo=UTC)
    return source_timestamp(value)


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure."""
    if not isinstance(value, str):
        return None
    out = html.unescape(value).strip()
    if not out:
        return None
    return out[:cap]
