"""Built In (https://builtin.com) — US tech jobs scraper.

Built In is a US-focused tech-jobs board where companies post directly
(not syndicated from LinkedIn / Indeed). The /jobs listing page embeds
a schema.org ``ItemList`` with the visible 30 jobs (per page) — title,
URL, and a one-line description for each — which we parse without any
JS rendering.

The library defaults to direct ``httpx`` fetching and pulls only what
the listing JSON-LD contains: title, URL, ats_id, and description. To
recover company / location / salary on the per-job detail pages
(which Built In renders client-side and is therefore invisible to a
plain HTTP GET) the user can opt into Firecrawl-based enrichment by
passing ``firecrawl_api_key="…"`` to the constructor or setting the
``FIRECRAWL_API_KEY`` env variable. Firecrawl is a paid service; the
scraper never calls it unless the user has explicitly enabled it.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://builtin.com"
DEFAULT_MAX_PAGES = 200
MAX_CONCURRENCY_LISTING = 4
# Enrichment is opt-in and per-job, so we cap it tighter to avoid
# hammering Firecrawl's per-minute quota when the user fires up the
# scraper.
MAX_CONCURRENCY_ENRICH = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
FIRECRAWL_BASE = "https://api.firecrawl.dev"

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
    - ``firecrawl_api_key`` — opt-in detail-page enrichment via the
      Firecrawl scraping service. Adds company / location / salary
      to each Job. Costs roughly $0.001 per job — billed to the key
      holder. Falls back to the ``FIRECRAWL_API_KEY`` env variable
      when omitted; if neither is set, enrichment is skipped and
      Jobs ship with title + URL + description only.
    """

    ats = ATSType.BUILTIN

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        firecrawl_api_key: str | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self.firecrawl_api_key = (
            firecrawl_api_key
            or os.environ.get("FIRECRAWL_API_KEY")
            or None
        )

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
                page_jobs = await self._fetch_listing_page(client, sem, page)
                new = sum(1 for j in page_jobs if j.ats_id not in seen)
                await absorb(page_jobs)
                consecutive_empty = 0 if new else consecutive_empty + 1
                page += 1

            if self.firecrawl_api_key:
                await self._enrich_via_firecrawl(client, jobs)

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
            description = _strip_html(description) or None

        return Job(
            url=url,
            title=title,
            company="Unknown",  # filled in by enrichment if enabled
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

    # --- optional Firecrawl enrichment --------------------------------------

    async def _enrich_via_firecrawl(
        self,
        client: httpx.AsyncClient,
        jobs: list[Job],
    ) -> None:
        """Fetch company / location / salary for each job via Firecrawl
        with a fixed extraction schema. The original Job is replaced
        in-place via ``model_copy``; failures fall through silently
        (the listing-level fields are still good enough on their own).
        """
        sem = asyncio.Semaphore(MAX_CONCURRENCY_ENRICH)

        async def enrich(idx: int, job: Job) -> None:
            data = await self._firecrawl_extract(client, sem, str(job.url))
            if not data:
                return
            updates: dict[str, Any] = {}
            if (company := data.get("company")) and isinstance(company, str):
                updates["company"] = company.strip() or job.company
            if (loc := data.get("location")) and isinstance(loc, str):
                updates["location"] = loc.strip() or job.location
            sal_min = data.get("salary_min")
            sal_max = data.get("salary_max")
            if isinstance(sal_min, (int, float)) and sal_min > 0:
                updates["salary_min"] = float(sal_min)
                updates["salary_currency"] = "USD"
                updates["salary_period"] = "YEAR"
            if isinstance(sal_max, (int, float)) and sal_max > 0:
                updates["salary_max"] = float(sal_max)
                updates["salary_currency"] = "USD"
                updates["salary_period"] = "YEAR"
            if updates:
                jobs[idx] = job.model_copy(update=updates)

        await asyncio.gather(*(enrich(i, j) for i, j in enumerate(jobs)))

    async def _firecrawl_extract(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> dict[str, Any] | None:
        body = {
            "url": url,
            "formats": ["extract"],
            "extract": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "location": {"type": "string"},
                        "salary_min": {"type": "number"},
                        "salary_max": {"type": "number"},
                    },
                },
            },
        }
        async with sem:
            try:
                r = await client.post(
                    f"{FIRECRAWL_BASE}/v1/scrape",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self.firecrawl_api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=60,
                )
            except httpx.HTTPError:
                return None
        if r.status_code != 200:
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        data = payload.get("data") or {}
        return data.get("extract") or data.get("json") or None


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
