"""Built In (https://builtin.com) — US tech jobs scraper.

Built In is a US-focused tech-jobs board where companies post directly
(not syndicated from LinkedIn / Indeed). The /jobs listing page embeds
a schema.org ``ItemList`` with the visible 30 jobs (per page) — title,
URL, and a one-line description for each — which we parse without any
JS rendering.

The scraper tries direct ``httpx`` first; on the 403 that builtin.com
serves to bare httpx user-agents (post-Cloudflare hardening, observed
2026-05-09) it switches to ``httpcloak`` for the rest of the fetch.
``httpcloak`` is a TLS+h2 fingerprint impersonator already shipped in
the ``scrapers`` extra and used by Avature/JazzHR/Eightfold; no extra
config or paid service involved.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://builtin.com"
DEFAULT_MAX_PAGES = 200
MAX_CONCURRENCY_LISTING = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Built In serves the JSON-LD with `&#x2B;` instead of '+' in the type
# attribute. Match either; one regex per page payload.
_LD_RE = re.compile(
    r'<script[^>]+type="application/ld(?:\+|&#x2B;)json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_URL_ID_RE = re.compile(r"^https?://[^/]+/job/[^/]+/(?P<id>\d+)/?$")


@ScraperRegistry.register(ATSType.BUILTIN)
class BuiltInScraper(BaseScraper):
    """Built In (builtin.com) — US tech jobs.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap (default 200, ~3,000-6,000 jobs
      depending on the listing density on each page).
    """

    ats = ATSType.BUILTIN

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        # Flipped to True the first time the direct ``httpx`` path
        # returns 403; subsequent requests in this scraper instance
        # then go through ``httpcloak``. Reset by re-instantiating.
        self._use_httpcloak = False

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for j in items:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY_LISTING)
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 3:
                try:
                    page_jobs = await self._fetch_listing_page(client, sem, page)
                except ScraperError as exc:
                    # Cloudflare and httpcloak both rate-limit deep
                    # pagination — once we hit a hard wall we keep what
                    # we already collected rather than throw it all out.
                    # Page 1 failures are still fatal (nothing to keep).
                    if page == 1:
                        raise
                    log.warning(
                        "Built In: stopping pagination at page %d (%s); "
                        "keeping %d jobs collected so far.",
                        page, exc, len(jobs),
                    )
                    break
                new = sum(1 for j in page_jobs if j.ats_id not in seen)
                await absorb(page_jobs)
                consecutive_empty = 0 if new else consecutive_empty + 1
                page += 1

        return jobs

    # --- listing pages ------------------------------------------------------

    async def _fetch_listing_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> list[Job]:
        url = f"{API_ROOT}/jobs?page={page}"
        text = await self._request_html(client, sem, url)
        return self._parse_listing(text)

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
            fetched_at=datetime.now(),
        )

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        # Once a 403 has flipped the instance to httpcloak mode, every
        # subsequent request skips the wasted direct attempt.
        if self._use_httpcloak:
            return await self._request_via_httpcloak(url)

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,*/*",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Built In fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 403:
                # Cloudflare-style block on the bare httpx fingerprint.
                # Flip the scraper into httpcloak mode and retry; every
                # subsequent page in this fetch reuses the cheap path.
                log.info(
                    "Built In: 403 on %s — switching to httpcloak fallback",
                    url,
                )
                self._use_httpcloak = True
                return await self._request_via_httpcloak(url)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Built In returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Built In returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"Built In exhausted retries for {url}: {last_exc}"
        )

    async def _request_via_httpcloak(self, url: str) -> str:
        """TLS+h2 impersonation fallback used when builtin.com 403's
        the direct httpx user-agent. Verified live 2026-05-09: 200 with
        full ~390 KB HTML where direct returns 403/919 B.

        Cloudflare also rate-limits deep pagination via httpcloak — the
        first 403 here is treated as transient (retry with backoff) and
        only escalates to a hard ``ScraperError`` if it survives every
        retry. The caller in :meth:`_fetch_async` then keeps the jobs
        collected so far rather than throwing them away.
        """
        from importlib.util import find_spec

        if find_spec("httpcloak") is None:
            raise ScraperError(
                "Built In's 403 fallback needs httpcloak — "
                "`pip install jobhive-py[scrapers]`."
            )

        last_status: int | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            text = await asyncio.to_thread(self._httpcloak_get_sync, url)
            if isinstance(text, str):
                return text
            # ``_httpcloak_get_sync`` returned the int status on non-200
            # so we can decide here whether to retry or escalate.
            last_status = text
            if last_status != 403 or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        raise ScraperError(
            f"Built In httpcloak fallback returned {last_status} for "
            f"{url} after {MAX_RETRIES} retries"
        )

    @staticmethod
    def _httpcloak_get_sync(url: str) -> str | int:
        """Sync fetch via httpcloak. Returns the page text on 200, the
        bare status int otherwise so the async caller can decide
        retry/escalate without raising for transient blocks."""
        import httpcloak

        r = httpcloak.get(url, timeout=30)
        if r.status_code != 200:
            return int(r.status_code)
        content = r.content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return content


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
