"""The Muse (https://www.themuse.com) — US-leaning direct-posting jobs.

The Muse is a US-focused career platform where companies pay to list
(not LinkedIn / Indeed syndication). The published API exposes a
``/api/public/jobs`` endpoint that paginates 20 jobs per page.

The site claims ~500k total jobs, but the public API caps deep
pagination at **page=99** (~2,000 jobs) regardless of filters — the
known undocumented ceiling. We scrape what's accessible (the most
recent ~2,000 postings) and accept the cap.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://www.themuse.com/api/public/jobs"
PER_PAGE = 20  # Hardcoded by the API.
PAGE_CEILING = 99  # Empirical: page=100 returns 400.
DEFAULT_MAX_PAGES = PAGE_CEILING + 1  # Pages are 0-indexed, 0..99 = 100 pages.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.THEMUSE)
class TheMuseScraper(BaseScraper):
    """The Muse (themuse.com) — US-leaning career platform.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap (default 100 pages = 2,000 jobs,
      the API's hard ceiling). Set lower for incremental runs.
    """

    ats = ATSType.THEMUSE

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        # Clamp at the API's hard ceiling so callers can't accidentally
        # push past it (>=100 → 400 from the server).
        self.max_pages = max(1, min(max_pages, DEFAULT_MAX_PAGES))

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

            async def one(page: int) -> None:
                payload = await self._fetch_page(client, sem, page=page)
                await absorb(payload.get("results") or [])

            await asyncio.gather(
                *(one(p) for p in range(self.max_pages))
            )
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params={"page": page}, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"The Muse fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"The Muse returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 400:
                # Past the deep-pagination cap — treat as 'no more pages'.
                return {"results": []}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"The Muse returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"The Muse returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"The Muse exhausted retries at page={page}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "")
        name = (item.get("name") or "").strip()
        if not ats_id or not name:
            return None

        company = (
            (item.get("company") or {}).get("name") or ""
        ).strip() or "Unknown"

        # Location: take the first non-empty location label. Multi-
        # location postings get the rest stashed in ``raw`` so they're
        # not lost.
        locs = item.get("locations") or []
        location_names = [
            entry.get("name") for entry in locs
            if isinstance(entry, dict) and entry.get("name")
        ]
        location = location_names[0] if location_names else None

        # The Muse's free-form level label (Internship / Entry Level / Senior
        # Level / Director / etc.) is surfaced as ``commitment`` since the
        # canonical seniority enum was dropped from the ``Job`` model.
        level_name: str | None = None
        levels = item.get("levels") or []
        for lvl in levels:
            if isinstance(lvl, dict) and lvl.get("name"):
                level_name = lvl["name"].strip()
                break

        url = ((item.get("refs") or {}).get("landing_page") or "").strip()
        if not url:
            return None

        description = _strip_html(item.get("contents") or "")
        posted_at = _parse_iso(item.get("publication_date"))

        raw: dict[str, Any] = {}
        if len(location_names) > 1:
            raw["additional_locations"] = location_names[1:6]
        cats = [
            c.get("name") for c in (item.get("categories") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        if cats:
            raw["categories"] = cats[:5]
        company_id = (item.get("company") or {}).get("id")
        if company_id:
            raw["company_id"] = str(company_id)

        return Job(
            url=url,
            title=name,
            company=company,
            ats_type=ATSType.THEMUSE,
            ats_id=ats_id,
            location=location,
            commitment=level_name,
            description=description or None,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:10_000]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
