"""Uber careers scraper.

    POST https://www.uber.com/api/loadSearchJobsResults?localeCode=en

Returns a paginated payload. The endpoint accepts a placeholder CSRF token.

**Important payload shape**: ``limit`` and ``page`` MUST be at the top level
of the request body — *not* inside ``params``. If they're nested under
``params``, the API silently ignores them and returns its default page
(1000 results) regardless of pagination, producing the same listings on
every call. We hit this bug pre-fix: 11K "jobs" returned for a tenant
with 1,077 actual openings, all duplicates.

Field names inside ``params``: ``lineOfBusinessName`` and ``programAndPlatform``
(NOT ``lineOfBusiness`` / ``program`` — empty arrays mask the typo).
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

API_URL = "https://www.uber.com/api/loadSearchJobsResults"
PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "regular": "FULL_TIME",
    "permanent": "FULL_TIME",
}


@ScraperRegistry.register(ATSType.UBER)
class UberScraper(BaseScraper):
    """Uber scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.UBER

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        page = 0
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            while True:
                data = await self._fetch_page(client, page=page)
                results = data.get("results") or []
                if not results:
                    break
                for item in results:
                    job = self._parse_job(item)
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    all_jobs.append(job)
                total = _extract_total(data)
                if (page + 1) * PAGE_SIZE >= total or len(results) < PAGE_SIZE:
                    break
                page += 1
        return all_jobs

    async def _fetch_page(
        self, client: httpx.AsyncClient, *, page: int
    ) -> dict[str, Any]:
        payload = {
            # `limit` and `page` MUST be at the top level — see module docstring.
            "limit": PAGE_SIZE,
            "page": page,
            "params": {
                "department": [],
                "lineOfBusinessName": [],
                "location": [],
                "programAndPlatform": [],
                "team": [],
            },
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(
                    API_URL,
                    params={"localeCode": "en"},
                    json=payload,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Content-Type": "application/json",
                        "x-csrf-token": "x",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Uber fetch failed at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.json().get("data") or {}
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Uber returned {response.status_code} at page={page} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Uber returned {response.status_code} at page={page}: "
                f"{response.text[:120]}"
            )
        raise ScraperError(f"Uber exhausted retries at page={page}")

    def _parse_job(self, item: dict[str, Any]) -> Job:
        ats_id = str(item.get("id") or "")
        all_locations = item.get("allLocations") or []
        first_loc = all_locations[0] if all_locations else (item.get("location") or {})
        location = None
        if isinstance(first_loc, dict):
            # Prefer ``countryName`` ("United Kingdom") over the
            # ISO-3 ``country`` code ("GBR") for human readability.
            parts = [
                first_loc.get("city"),
                first_loc.get("region"),
                first_loc.get("countryName") or first_loc.get("country"),
            ]
            location = ", ".join(p for p in parts if p) or None
        elif isinstance(first_loc, str):
            location = first_loc

        # Description is markdown — kept verbatim, capped at 25k chars.
        description_raw = item.get("description")
        description = (
            description_raw.strip()[:25_000] or None
            if isinstance(description_raw, str)
            else None
        )

        # ``timeType`` ships as ``"Full-Time"`` / ``"Part-Time"`` /
        # ``"Intern"`` / ``"Contract"``. Map to the canonical enum.
        time_type = item.get("timeType")
        commitment = (
            time_type.strip() if isinstance(time_type, str) and time_type.strip()
            else None
        )
        employment_type: str | None = None
        if commitment:
            norm = commitment.lower()
            for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                if needle in norm:
                    employment_type = mapped
                    break

        raw: dict[str, Any] = {}
        for k in ("department", "team", "category", "subCategory",
                  "level", "otherLevels", "remote", "allLocations",
                  "programAndPlatform", "type", "timeType", "uniqueSkills"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=f"https://www.uber.com/global/en/careers/list/{ats_id}/",
            title=item.get("title") or "Untitled",
            company="Uber",
            ats_type=ATSType.UBER,
            ats_id=ats_id,
            location=location,
            department=item.get("department") if isinstance(item.get("department"), str) else None,
            team=item.get("team") if isinstance(item.get("team"), str) else None,
            employment_type=employment_type,
            commitment=commitment,
            description=description,
            requisition_id=ats_id if ats_id else None,
            posted_at=_parse_iso(
                item.get("creationDate")
                or item.get("createdDate")
                or item.get("updatedDate")
            ),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _extract_total(data: dict[str, Any]) -> int:
    """Uber's `totalResults` is a 64-bit `{"low", "high", "unsigned"}`
    envelope. The 32-bit ``low`` field is enough — Uber doesn't have 4B+
    job postings."""
    envelope = data.get("totalResults")
    if isinstance(envelope, dict):
        return int(envelope.get("low") or 0)
    if isinstance(envelope, (int, float)):
        return int(envelope)
    return 0


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
