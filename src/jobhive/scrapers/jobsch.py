"""jobs.ch — Switzerland's largest direct-posting job board (~50k active).

Companies pay to list on jobs.ch — postings are not syndicated from
LinkedIn / Indeed. Coverage spans all of Switzerland (DE-CH, FR-CH,
IT-CH, EN) across every sector (the API doesn't restrict to tech).
The May 2026 audit had Switzerland at 0.2% of the dataset; this is
roughly a 25× lift.

Public REST API at ``https://www.jobs.ch/api/v1/public/search`` — no
auth, no key. Pagination is ``?start=N&rows=20`` (rows hard-capped
at 20; >20 → 422). Each entry has ``company_name`` embedded so no
separate company-resolution fetch is needed. The detail-page URL
template is in ``_links.detail_*`` (German is the canonical default).

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://www.jobs.ch/api/v1/public/search"
PER_PAGE = 20  # API hard-caps ``rows`` at 20 (>20 → 422).
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
# Default cap on total pages to fetch — 2,500 pages × 20 jobs = full
# 50k inventory. Set lower via ``max_pages`` for incremental runs.
DEFAULT_MAX_PAGES = 2500


@ScraperRegistry.register(ATSType.JOBSCH)
class JobsChScraper(BaseScraper):
    """jobs.ch (Switzerland) — direct-posting board.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap (default 2,500, ~50k jobs).
    """

    ats = ATSType.JOBSCH

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # First request to learn the real total. The API doesn't
            # ship total in a single field on every response shape; we
            # use ``total_hits`` from page 1 as the planning anchor.
            first = await self._fetch_page(client, sem, start=0)
            total = int(first.get("total_hits") or 0)
            await absorb(first.get("documents") or [])

            if total <= PER_PAGE:
                return jobs

            page_count = min(
                (total + PER_PAGE - 1) // PER_PAGE, self.max_pages
            )
            offsets = [PER_PAGE * i for i in range(1, page_count)]

            async def one(offset: int) -> None:
                payload = await self._fetch_page(client, sem, start=offset)
                await absorb(payload.get("documents") or [])

            await asyncio.gather(*(one(o) for o in offsets))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        start: int,
    ) -> dict[str, Any]:
        params = {"start": start, "rows": PER_PAGE}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"jobs.ch fetch failed at start={start}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"jobs.ch returned non-JSON at start={start}: {exc}"
                    ) from exc
            if response.status_code == 422:
                # Past the search-engine cap (rare; API caps deep
                # pagination differently per query). Treat as exhausted.
                return {"documents": [], "total_hits": 0}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"jobs.ch returned {response.status_code} at "
                        f"start={start} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"jobs.ch returned {response.status_code} at start={start}"
            )
        raise ScraperError(
            f"jobs.ch exhausted retries at start={start}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("job_id") or "")
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not ats_id or not title:
            return None

        url = _detail_url(item, ats_id)

        # ``place`` is the city name; ``regions`` is a numeric path
        # (cantons + sub-regions) we don't have a name table for. The
        # city is enough for downstream geo-search.
        place = (item.get("place") or "").strip() or None
        location = f"{place}, Switzerland" if place else "Switzerland"

        # employment_grades is a list like [100] (% time). When the
        # only value is below 100 the role is part-time; when 100 it's
        # full-time; mixed lists indicate flexibility.
        grades = item.get("employment_grades") or []
        is_full_time = grades == [100]
        employment_type = "FULL_TIME" if is_full_time else (
            "PART_TIME" if grades and all(g < 100 for g in grades) else None
        )

        posted_at = _parse_iso(
            item.get("publication_date") or item.get("initial_publication_date")
        )

        raw: dict[str, Any] = {}
        if grades:
            raw["employment_grades"] = grades
        languages = [
            entry.get("language") for entry in (item.get("language_skills") or [])
            if isinstance(entry, dict) and entry.get("language")
        ]
        if languages:
            raw["languages"] = languages
        if item.get("company_id"):
            raw["company_id"] = str(item["company_id"])
        if item.get("company_segmentation"):
            raw["company_segmentation"] = item["company_segmentation"]

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.JOBSCH,
            ats_id=ats_id,
            location=location,
            employment_type=employment_type,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _detail_url(item: dict[str, Any], job_id: str) -> str:
    """Prefer ``_links.detail_{lang}.href`` when present (jobs.ch ships
    a localized detail URL per row), else fall back to the documented
    canonical English URL pattern.
    """
    links = item.get("_links") or {}
    if isinstance(links, dict):
        for key in ("detail_en", "detail_de", "detail_fr", "detail_it"):
            entry = links.get(key)
            if isinstance(entry, dict):
                href = entry.get("href")
                if isinstance(href, str) and href:
                    return href
    return f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
