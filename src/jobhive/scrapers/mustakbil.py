"""Mustakbil — Pakistan direct employer job board public API."""

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

API_URL = "https://api-public.mustakbil.com/ws/jobs/search/"
DEFAULT_MAX_PAGES = 500
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
PAKISTAN_COUNTRY_ID = 162


@ScraperRegistry.register(ATSType.MUSTAKBIL)
class MustakbilScraper(BaseScraper):
    """Mustakbil Pakistan jobs scraper."""

    ats = ATSType.MUSTAKBIL

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        country_id: int = PAKISTAN_COUNTRY_ID,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self.country_id = country_id

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page in range(1, self.max_pages + 1):
                try:
                    payload = await self._fetch_page(client, page=page)
                except ScraperError:
                    if page == 1:
                        raise
                    break
                items = payload.get("list") or []
                if not items:
                    break
                new_count = 0
                for item in items:
                    job = self._parse(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1
                if new_count == 0:
                    break
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        *,
        page: int,
    ) -> dict[str, Any]:
        params = {"countryid": self.country_id, "page": page}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    API_URL,
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                        "Origin": "https://www.mustakbil.com",
                        "Referer": "https://www.mustakbil.com/jobs/pakistan",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Mustakbil fetch failed at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Mustakbil returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Mustakbil returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Mustakbil returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"Mustakbil exhausted retries at page={page}: {last_exc}")

    def _parse(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        title = (item.get("title") or "").strip()
        if raw_id is None or not title:
            return None

        salary_min = _to_float(item.get("salaryMin"))
        salary_max = _to_float(item.get("salaryMax"))
        currency = item.get("currency")
        salary_summary = None
        if currency and (salary_min is not None or salary_max is not None):
            salary_summary = f"{salary_min or ''}-{salary_max or ''} {currency}".strip()

        raw: dict[str, Any] = {}
        for key in (
            "employerId",
            "category",
            "shift",
            "experienceLevel",
            "adType",
            "vacancies",
            "lastDate",
        ):
            value = item.get(key)
            if value not in (None, ""):
                raw[key] = value

        return Job(
            url=_job_url(item, str(raw_id)),
            title=title,
            company=(item.get("company") or "").strip() or "Unknown",
            ats_type=ATSType.MUSTAKBIL,
            ats_id=str(raw_id),
            location=_join_nonempty(item.get("cities") or item.get("city"), item.get("country")),
            country_iso="PK" if item.get("country") == "Pakistan" else None,
            is_remote=True if item.get("telecommute") is True else None,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency if isinstance(currency, str) and len(currency) == 3 else None,
            salary_summary=salary_summary,
            employment_type=_map_employment_type(item.get("type")),
            commitment=item.get("type") or None,
            department=item.get("category") or None,
            description=item.get("description") or None,
            posted_at=_parse_ts(item.get("postedOn")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _job_url(item: dict[str, Any], ats_id: str) -> str:
    url_title = item.get("urlTitle")
    country = item.get("urlCountry") or "pakistan"
    city = item.get("urlCity") or ""
    if isinstance(url_title, str) and url_title:
        city_part = f"/{city}" if city else ""
        return f"https://www.mustakbil.com/jobs/job/{ats_id}/{country}{city_part}/{url_title}"
    return f"https://www.mustakbil.com/jobs/job/{ats_id}"


def _join_nonempty(*values: object) -> str | None:
    parts = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return ", ".join(parts) if parts else None


def _map_employment_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    norm = value.lower()
    if "intern" in norm:
        return "INTERN"
    if "part" in norm:
        return "PART_TIME"
    if "contract" in norm or "freelance" in norm:
        return "CONTRACT"
    if "temporary" in norm:
        return "TEMPORARY"
    if "full" in norm:
        return "FULL_TIME"
    return None


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
