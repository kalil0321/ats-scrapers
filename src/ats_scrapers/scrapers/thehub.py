"""The Hub (https://thehub.io) — Nordic startup jobs scraper.

The Hub is the leading direct-posting tech / startup job platform
for the Nordics (DK, SE, NO, FI). Companies pay to list — not
syndicated from LinkedIn / Indeed. ~1,000 active postings at any one
time, all developer- or growth-focused, all with structured
location + lat/lon + apply URL data.

Public REST at ``https://thehub.io/api/jobs`` — no auth, no key.
Pagination via ``?page=N`` (15 docs per page; page count is in the
response envelope).

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://thehub.io/api/jobs"
JOB_URL_TEMPLATE = "https://thehub.io/jobs/{job_id}"
PER_PAGE = 15  # Hard-coded by the API; ?limit=… is ignored.
MAX_CONCURRENCY = 4

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.THEHUB)
class TheHubScraper(BaseScraper):
    """The Hub (thehub.io) — Nordic startup jobs.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``max_pages`` — pagination cap (default 200, far above the
      ~70 pages currently in the active board).
    """

    ats = ATSType.THEHUB
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int = 200,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.max_pages = max_pages

    async def afetch(self) -> list[Job]:
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

        async with self.make_fetcher() as fetch:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Probe page 1 to learn ``pages`` count.
            first = await self._fetch_page(fetch, sem, page=1)
            pages_total = int(first.get("pages") or 1)
            await absorb(first.get("docs") or [])

            page_count = min(pages_total, self.max_pages)
            if page_count <= 1:
                return jobs

            async def one(page: int) -> None:
                payload = await self._fetch_page(fetch, sem, page=page)
                await absorb(payload.get("docs") or [])

            await asyncio.gather(*(one(p) for p in range(2, page_count + 1)))
        return jobs

    async def _fetch_page(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        async with sem:
            return await fetch.get_json(API_URL, params={"page": page})

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = (item.get("id") or item.get("_id") or "").strip()
        title = (item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        # Filter to ACTIVE rows. The API also returns DRAFT / EXPIRED
        # postings depending on cache state.
        if (item.get("status") or "").upper() != "ACTIVE":
            return None

        company_obj = item.get("company") or {}
        company = (company_obj.get("name") or "").strip() or "Unknown"

        location_obj = item.get("location") or {}
        location = _format_location(location_obj)

        lat, lon = _extract_lat_lon(item.get("geoLocation") or {})

        is_remote = bool(item.get("isRemote"))
        description = _clean_description(item.get("description"))
        posted_at = _parse_iso(
            item.get("publishedAt") or item.get("approvedAt")
            or item.get("createdAt")
        )

        # The Hub's ``link`` field is the apply URL (often a Workable /
        # Greenhouse / etc. handoff). Keep the ``thehub.io/jobs/{id}``
        # canonical URL as the primary url; stash apply elsewhere.
        # Some postings ship link='' — must reject those before the
        # Pydantic HttpUrl validator does (it rejects empty strings).
        apply_raw = item.get("link")
        apply_url = (
            apply_raw.strip() if isinstance(apply_raw, str) else None
        )
        if not apply_url or not apply_url.startswith(("http://", "https://")):
            apply_url = None

        # Salary: API ships either a string ('competitive', 'undisclosed')
        # or a salaryRange object. We only set the canonical fields when
        # we have numeric values.
        salary_min, salary_max, salary_currency = _parse_salary(
            item.get("salary"), item.get("salaryRange"),
        )

        raw: dict[str, Any] = {}
        if item.get("equity"):
            raw["equity"] = item["equity"]
        if item.get("countryCode"):
            raw["country_code"] = item["countryCode"]
        roles = item.get("jobRoles")
        if isinstance(roles, list) and roles:
            raw["job_role_ids"] = roles[:10]
        position_types = item.get("jobPositionTypes")
        if isinstance(position_types, list) and position_types:
            raw["job_position_type_ids"] = position_types[:5]

        return Job(
            url=JOB_URL_TEMPLATE.format(job_id=ats_id),
            title=title,
            company=company,
            ats_type=ATSType.THEHUB,
            ats_id=ats_id,
            location=location,
            lat=lat,
            lon=lon,
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period="YEAR" if salary_currency else None,
            salary_min=salary_min,
            salary_max=salary_max,
            apply_url=apply_url,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _format_location(loc: dict[str, Any]) -> str | None:
    """``location`` ships as {address, locality, country}. Prefer the
    full address; fall back to locality + country."""
    address = (loc.get("address") or "").strip()
    if address:
        return address
    parts = [
        (loc.get(k) or "").strip()
        for k in ("locality", "country") if isinstance(loc.get(k), str)
    ]
    parts = [p for p in parts if p]
    return ", ".join(parts) or None


def _extract_lat_lon(geo: dict[str, Any]) -> tuple[float | None, float | None]:
    """``geoLocation.center.coordinates`` is GeoJSON: ``[lon, lat]`` —
    GeoJSON uses lon-first, our model uses lat-first. Swap on return."""
    center = geo.get("center") or {}
    coords = center.get("coordinates") if isinstance(center, dict) else None
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            return None, None
        return lat, lon
    return None, None


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:25_000] or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_salary(
    salary: object, salary_range: object,
) -> tuple[float | None, float | None, str | None]:
    """``salary`` is a free-text label ('competitive', 'undisclosed',
    sometimes a real number); ``salaryRange`` is structured. Prefer
    the structured object, ignore the label when it isn't numeric."""
    if isinstance(salary_range, dict):
        lo = salary_range.get("from") or salary_range.get("min") or salary_range.get("low")
        hi = salary_range.get("to") or salary_range.get("max") or salary_range.get("high")
        cur = salary_range.get("currency")
        lo_f = _to_pos_float(lo)
        hi_f = _to_pos_float(hi)
        if lo_f or hi_f:
            return lo_f, hi_f, (cur if isinstance(cur, str) and cur else None)
    return None, None, None


def _to_pos_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            v = float(value)
            return v if v > 0 else None
        except ValueError:
            return None
    return None
