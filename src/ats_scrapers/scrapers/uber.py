"""Uber careers scraper.

Uber retired its legacy ``/api/loadSearchJobsResults`` RPC in August 2026.
Its replacement careers site exposes the live catalogue through the public
``/api/jobs/search/`` route used by the browser. Cloudflare blocks plain HTTP
clients on that route, so the scraper uses the project's existing
TLS-impersonating ``httpcloak`` transport rather than a browser runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urljoin

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://jobs.uber.com/api/jobs/search/"
JOBS_ORIGIN = "https://jobs.uber.com"
PAGE_SIZE = 1000

_EMPLOYMENT_TYPE_PATTERNS: dict[str, EmploymentType] = {
    "intern": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
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

_SALARY_PERIODS: dict[str, SalaryPeriod] = {
    "hour": "HOUR",
    "hourly": "HOUR",
    "day": "DAY",
    "daily": "DAY",
    "week": "WEEK",
    "weekly": "WEEK",
    "month": "MONTH",
    "monthly": "MONTH",
    "year": "YEAR",
    "yearly": "YEAR",
    "annual": "YEAR",
    "annually": "YEAR",
}


@ScraperRegistry.register(ATSType.UBER)
class UberScraper(BaseScraper):
    """Uber scraper — ``company_slug`` is informational; jobs are global."""

    ats = ATSType.UBER
    fetch_engine = "cloak"

    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "Referer": "https://jobs.uber.com/en/jobs/",
    }

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            first_page = await self._fetch_page(fetch, page=1)
            total = _positive_int(first_page.get("totalJobs"), "totalJobs")
            total_pages = _positive_int(first_page.get("totalPages"), "totalPages")
            items = list(first_page["jobs"])

            for page in range(2, total_pages + 1):
                payload = await self._fetch_page(fetch, page=page)
                page_total = _positive_int(payload.get("totalJobs"), "totalJobs")
                if page_total != total:
                    raise ScraperError(
                        f"Uber careers total changed during pagination: "
                        f"{total} -> {page_total}"
                    )
                items.extend(payload["jobs"])

        jobs_by_id: dict[str, Job] = {}
        for item in items:
            job = self._parse_job(item)
            job_id = job.ats_id
            if not job_id:
                raise ScraperError("Uber careers parsed a job without an ID")
            if job_id in jobs_by_id:
                raise ScraperError(f"Uber careers returned duplicate job ID {job_id}")
            jobs_by_id[job_id] = job

        if len(jobs_by_id) != total:
            raise ScraperError(
                f"Uber careers advertised {total} jobs but returned "
                f"{len(jobs_by_id)} unique IDs"
            )
        return list(jobs_by_id.values())

    async def _fetch_page(self, fetch: Fetcher, *, page: int) -> dict[str, Any]:
        payload = await fetch.get_json(
            API_URL,
            params={"page": page, "pagesize": PAGE_SIZE},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ScraperError("Uber careers returned an invalid jobs payload")
        if not payload["jobs"]:
            raise ScraperError(f"Uber careers page {page} returned no jobs")
        return payload

    def _parse_job(self, item: Any) -> Job:
        if not isinstance(item, dict):
            raise ScraperError("Uber careers returned a non-object job")

        job_id = _text(item.get("Id"))
        title = _text(item.get("Title"))
        if not job_id or not title:
            raise ScraperError("Uber careers returned a job without an ID or title")

        locations = item.get("Locations")
        location_items = (
            [value for value in locations if isinstance(value, dict)]
            if isinstance(locations, list)
            else []
        )
        first_location = location_items[0] if location_items else {}
        location_parts = [
            value for value in (_location_text(item) for item in location_items) if value
        ]
        location = "; ".join(dict.fromkeys(location_parts)) or None
        country_iso = _text(first_location.get("CountryCode"))
        if country_iso and len(country_iso) != 2:
            country_iso = None

        coordinates = None
        point = first_location.get("LocationPoint")
        if isinstance(point, dict) and isinstance(point.get("coordinates"), list):
            coordinates = point["coordinates"]
        lon = _number(coordinates[0]) if coordinates and len(coordinates) >= 2 else None
        lat = _number(coordinates[1]) if coordinates and len(coordinates) >= 2 else None

        teams_value = item.get("Teams")
        teams = (
            [value for value in (_text(team) for team in teams_value) if value]
            if isinstance(teams_value, list)
            else []
        )
        contract_type = _text(item.get("ContractType"))
        work_pattern = _text(item.get("WorkPattern"))
        commitment_parts = list(dict.fromkeys(filter(None, (contract_type, work_pattern))))
        commitment = " / ".join(commitment_parts) or None

        salary_value = item.get("Salary")
        salary: dict[str, Any] = salary_value if isinstance(salary_value, dict) else {}
        currency = _text(salary.get("Currency"))
        if currency:
            currency = currency.upper()
            if len(currency) != 3:
                currency = None

        url = _job_url(item.get("Urls"), job_id)
        raw = {
            key: value
            for key, value in item.items()
            if key
            in {
                "AdditionalText",
                "ContractType",
                "ExperienceLevel",
                "Locations",
                "Remote",
                "Salary",
                "Summary",
                "Teams",
                "WorkPattern",
            }
            and value not in (None, "", [], {})
        }

        return Job(
            url=url,
            title=title,
            company="Uber",
            ats_type=ATSType.UBER,
            ats_id=job_id,
            location=location,
            country_iso=country_iso.upper() if country_iso else None,
            region=None,
            lat=lat,
            lon=lon,
            is_remote=item.get("Remote") if isinstance(item.get("Remote"), bool) else None,
            salary_currency=currency,
            salary_period=_salary_period(salary.get("Period")),
            salary_summary=_text(salary.get("Description")),
            salary_min=_number(salary.get("MinValue")),
            salary_max=_number(salary.get("MaxValue")),
            employment_type=_employment_type(commitment),
            department=_text(item.get("AdditionalText")),
            team=", ".join(teams) or None,
            requisition_id=_text(item.get("Reference")) or job_id,
            commitment=commitment,
            description=(
                _html_to_text(item.get("Description"))
                if self.include_descriptions
                else None
            ),
            posted_at=_parse_iso(item.get("DisplayDate")),
            fetched_at=datetime.now(UTC),
            language="en",
            raw=raw or None,
        )


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScraperError(f"Uber careers returned invalid {field}") from exc
    if parsed <= 0:
        raise ScraperError(f"Uber careers returned invalid {field}")
    return parsed


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _location_text(location: dict[str, Any]) -> str | None:
    address = _text(location.get("Address"))
    if address:
        return address
    parts = [
        _text(location.get("City")),
        _text(location.get("Region")),
        _text(location.get("Country")),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part)) or None


def _job_url(urls: Any, job_id: str) -> HttpUrl:
    if isinstance(urls, list):
        candidates = [value for value in urls if isinstance(value, dict)]
        default = next((value for value in candidates if value.get("IsDefault") is True), None)
        selected = default or (candidates[0] if candidates else None)
        path = _text(selected.get("Url")) if selected else None
        if path:
            return HttpUrl(urljoin(JOBS_ORIGIN, path))
    return HttpUrl(f"{JOBS_ORIGIN}/en/jobs/{job_id}/")


def _employment_type(commitment: str | None) -> EmploymentType | None:
    normalized = commitment.lower() if commitment else ""
    return next(
        (mapped for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items() if needle in normalized),
        None,
    )


def _salary_period(value: Any) -> SalaryPeriod | None:
    normalized = _text(value)
    return _SALARY_PERIODS.get(normalized.lower()) if normalized else None


def _html_to_text(value: Any) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            "Uber scraping requires beautifulsoup4; install ats-scrapers[scrapers]"
        ) from exc
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)[:25_000] or None


def _parse_iso(value: Any) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
