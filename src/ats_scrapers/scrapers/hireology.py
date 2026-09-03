"""Hireology public career-site scraper."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

CAREERS_URL = "https://careers.hireology.com/{tenant}/"
LISTING_API_URL = "https://api.hireology.com/v2/public/careers/{tenant}"
PAGE_SIZE = 1_000
_STARTING_DATA_MARKER = "var startingData ="
_UNSAFE_PATH_CHARS_RE = re.compile(r"[/\\?#\x00-\x1f\x7f]")
_UNSAFE_REPORTED_SEGMENT_CHARS_RE = re.compile(r"[\\?#\x00-\x1f\x7f]")
_US_REGIONS = {
    "AK",
    "AL",
    "AR",
    "AS",
    "AZ",
    "CA",
    "CO",
    "CT",
    "DC",
    "DE",
    "FL",
    "GA",
    "GU",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MP",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "PR",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UM",
    "UT",
    "VA",
    "VI",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
}
_CANADA_REGIONS = {
    "AB",
    "BC",
    "MB",
    "NB",
    "NL",
    "NS",
    "NT",
    "NU",
    "ON",
    "PE",
    "QC",
    "SK",
    "YT",
}
_EMPLOYMENT_PATTERNS: tuple[tuple[str, EmploymentType], ...] = (
    ("intern", "INTERN"),
    ("part time", "PART_TIME"),
    ("part-time", "PART_TIME"),
    ("full time", "FULL_TIME"),
    ("full-time", "FULL_TIME"),
    ("contract", "CONTRACT"),
    ("temporary", "TEMPORARY"),
)
_SALARY_PERIODS: dict[str, SalaryPeriod] = {
    "hour": "HOUR",
    "day": "DAY",
    "week": "WEEK",
    "month": "MONTH",
    "year": "YEAR",
}


@ScraperRegistry.register(ATSType.HIREOLOGY)
class HireologyScraper(BaseScraper):
    """Scrape one public Hireology employer portal."""

    ats = ATSType.HIREOLOGY
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json,text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = _require_path_segment(
            company_slug,
            provider="HireologyScraper",
        ).casefold()
        self._quoted_slug = quote(self.company_slug, safe="-._~")
        self.company_name = _clean_text(company_name)

    async def afetch(self) -> list[Job]:
        careers_url = CAREERS_URL.format(tenant=self._quoted_slug)
        async with self.make_fetcher() as page_fetch:
            page = await page_fetch.get_text(careers_url)
        token, page_company = _portal_identity(page, self.company_slug)
        api_headers = {
            **self.default_headers,
            "Authorization": f"Bearer {token}",
        }
        async with self.make_fetcher(headers=api_headers) as api_fetch:
            items = await self._fetch_all(api_fetch)
        company = self.company_name or page_company
        jobs: list[Job] = []
        seen: set[str] = set()
        for item in items:
            job = self._parse_job(item, fallback_company=company)
            if job.ats_id in seen:
                raise ScraperError(
                    f"Hireology ({self.company_slug}) returned duplicate "
                    f"job id {job.ats_id!r}"
                )
            seen.add(str(job.ats_id))
            jobs.append(job)
        return jobs

    async def _fetch_all(self, fetch: Any) -> list[dict[str, Any]]:
        first = await fetch.get_json(
            LISTING_API_URL.format(tenant=self._quoted_slug),
            params={"page": 1, "page_size": PAGE_SIZE},
        )
        items, expected = _listing_page(
            first,
            tenant=self.company_slug,
            expected_page=1,
        )
        all_items = list(items)
        page = 2
        while len(all_items) < expected:
            payload = await fetch.get_json(
                LISTING_API_URL.format(tenant=self._quoted_slug),
                params={"page": page, "page_size": PAGE_SIZE},
            )
            page_items, page_expected = _listing_page(
                payload,
                tenant=self.company_slug,
                expected_page=page,
            )
            if page_expected != expected:
                raise ScraperError(
                    f"Hireology ({self.company_slug}) listing count changed "
                    f"from {expected} to {page_expected} during pagination"
                )
            if not page_items:
                raise ScraperError(
                    f"Hireology ({self.company_slug}) pagination stopped at "
                    f"{len(all_items)} of {expected} jobs"
                )
            all_items.extend(page_items)
            page += 1
        if len(all_items) != expected:
            raise ScraperError(
                f"Hireology ({self.company_slug}) expected {expected} jobs, "
                f"received {len(all_items)}"
            )
        return all_items

    def _parse_job(
        self,
        item: dict[str, Any],
        *,
        fallback_company: str,
    ) -> Job:
        job_id = _required_text(item.get("id"), "job id", self.company_slug)
        title = _required_text(item.get("name"), "title", self.company_slug)
        status = _required_text(item.get("status"), "status", self.company_slug)
        if status.casefold() != "open":
            raise ScraperError(
                f"Hireology ({self.company_slug}) returned non-open job "
                f"{job_id!r} with status {status!r}"
            )
        fallback_url = (
            f"https://careers.hireology.com/{self._quoted_slug}/"
            f"{job_id}/description"
        )
        reported_url = _clean_text(item.get("career_site_url"))
        canonical_url = (
            _canonical_reported_url(reported_url, job_id=job_id)
            if reported_url
            else fallback_url
        )
        if canonical_url is None:
            raise ScraperError(
                f"Hireology ({self.company_slug}) job {job_id!r} returned "
                f"unexpected URL {reported_url!r}"
            )

        organization = item.get("organization")
        if not isinstance(organization, dict):
            organization = {}
        company = _clean_text(organization.get("name")) or fallback_company
        locations = item.get("locations")
        if not isinstance(locations, list):
            locations = []
        location = _location(locations, remote=item.get("remote"))
        country_iso = _country_iso(locations)
        compensation = item.get("compensation")
        if not isinstance(compensation, dict):
            compensation = {}
        salary_min, salary_max = _salary_amounts(compensation)
        salary_period = _salary_period(compensation.get("comp_period"))
        salary_summary = _salary_summary(
            salary_min,
            salary_max,
            salary_period,
        )
        has_salary = salary_min is not None or salary_max is not None
        employment = _clean_text(item.get("employment_status"))
        job_family = item.get("job_family")
        if not isinstance(job_family, dict):
            job_family = {}
        description = (
            _html_text(item.get("job_description"))
            if self.include_descriptions
            else None
        )
        raw = {
            "organization_id": organization.get("id"),
            "organization_type": _clean_text(organization.get("type")),
            "job_family_id": job_family.get("id"),
            "application_path": _clean_text(item.get("application_path")),
            "application_basic": item.get("application_basic"),
            "blind_posted": item.get("blind_posted"),
            "compensation_frequency": _clean_text(
                compensation.get("comp_frequency")
            ),
        }
        return Job(
            url=canonical_url,
            title=title,
            company=company,
            ats_type=ATSType.HIREOLOGY,
            ats_id=job_id,
            location=location,
            country_iso=country_iso,
            region="North America" if country_iso in {"CA", "US"} else None,
            is_remote=(
                item.get("remote")
                if isinstance(item.get("remote"), bool)
                else None
            ),
            salary_currency=(
                "CAD"
                if has_salary and country_iso == "CA"
                else "USD"
                if has_salary and country_iso == "US"
                else None
            ),
            salary_period=salary_period if has_salary else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=_employment_type(employment),
            department=_clean_text(job_family.get("name")),
            requisition_id=job_id,
            apply_url=canonical_url,
            commitment=employment,
            description=description[:25_000] if description else None,
            posted_at=_parse_datetime(item.get("created_at")),
            fetched_at=datetime.now(UTC),
            raw=raw,
        )


def _portal_identity(page: str, tenant: str) -> tuple[str, str]:
    marker_index = page.find(_STARTING_DATA_MARKER)
    if marker_index < 0:
        raise CompanyNotFoundError(
            f"Hireology tenant has no public careers site: {tenant}"
        )
    json_start = marker_index + len(_STARTING_DATA_MARKER)
    remainder = page[json_start:].lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(remainder)
    except (json.JSONDecodeError, TypeError) as error:
        raise ScraperError(
            f"Hireology ({tenant}) returned malformed public configuration"
        ) from error
    if not isinstance(data, dict):
        raise ScraperError(
            f"Hireology ({tenant}) returned malformed public configuration"
        )
    careers_path = _clean_text(data.get("careersPath"))
    if careers_path is None or careers_path.casefold() != tenant:
        raise ScraperError(
            f"Hireology ({tenant}) configuration identified "
            f"{careers_path!r}"
        )
    token = _clean_text(data.get("apiToken"))
    if token is None or token.count(".") != 2:
        raise ScraperError(
            f"Hireology ({tenant}) returned no anonymous API token"
        )
    soup = BeautifulSoup(page, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    company = title.removeprefix("Jobs for ").strip() or tenant
    return token, company


def _require_path_segment(value: str, *, provider: str) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 200
        or _UNSAFE_PATH_CHARS_RE.search(cleaned)
    ):
        raise ScraperError(
            f"{provider}: invalid tenant slug {value!r} — expected one "
            "public URL path segment"
        )
    return cleaned


def _canonical_reported_url(
    url: str,
    *,
    job_id: str,
) -> str | None:
    parsed = urlparse(url)
    segments = [
        unquote(segment)
        for segment in parsed.path.split("/")
        if segment
    ]
    if not (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "careers.hireology.com"
        and len(segments) == 3
        and segments[1] == job_id
        and segments[2].casefold() == "description"
        and not _UNSAFE_REPORTED_SEGMENT_CHARS_RE.search(segments[0])
    ):
        return None
    return (
        "https://careers.hireology.com/"
        f"{quote(segments[0].casefold(), safe='-._~')}/"
        f"{job_id}/description"
    )


def _listing_page(
    payload: Any,
    *,
    tenant: str,
    expected_page: int,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict):
        raise ScraperError(
            f"Hireology ({tenant}) returned malformed listing data"
        )
    items = payload.get("data")
    count = payload.get("count")
    page = payload.get("page")
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        for item in items
    ):
        raise ScraperError(
            f"Hireology ({tenant}) returned no jobs list"
        )
    if not isinstance(count, int) or count < 0:
        raise ScraperError(
            f"Hireology ({tenant}) returned invalid job count {count!r}"
        )
    if page != expected_page:
        raise ScraperError(
            f"Hireology ({tenant}) returned page {page!r}, "
            f"expected {expected_page}"
        )
    return items, count


def _required_text(value: object, field: str, tenant: str) -> str:
    cleaned = _clean_text(value)
    if cleaned:
        return cleaned
    raise ScraperError(f"Hireology ({tenant}) job has no {field}")


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _html_text(value: object) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)


def _location(
    locations: list[object],
    *,
    remote: object,
) -> str | None:
    rendered: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        text = ", ".join(
            value
            for value in (
                _clean_text(location.get("address")),
                _clean_text(location.get("city")),
                _clean_text(location.get("state")),
                _clean_text(location.get("zip_code")),
            )
            if value
        )
        if text and text not in rendered:
            rendered.append(text)
    if rendered:
        return "; ".join(rendered)
    return "Remote" if remote is True else None


def _country_iso(locations: list[object]) -> str | None:
    states = {
        state.upper()
        for location in locations
        if isinstance(location, dict)
        if (state := _clean_text(location.get("state")))
    }
    if states and states <= _US_REGIONS:
        return "US"
    if states and states <= _CANADA_REGIONS:
        return "CA"
    return None


def _employment_type(value: str | None) -> EmploymentType | None:
    normalized = (value or "").replace("_", " ").casefold()
    return next(
        (
            mapped
            for marker, mapped in _EMPLOYMENT_PATTERNS
            if marker in normalized
        ),
        None,
    )


def _salary_amounts(
    compensation: dict[str, Any],
) -> tuple[float | None, float | None]:
    minimum = _amount(compensation.get("comp_range_min"))
    maximum = _amount(compensation.get("comp_range_max"))
    single = _amount(compensation.get("comp_single_amount"))
    if compensation.get("is_comp_range") is True:
        return minimum, maximum
    return (single, single) if single is not None else (None, None)


def _amount(value: object) -> float | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        amount = float(cleaned.replace(",", ""))
    except ValueError:
        return None
    return amount if amount > 0 else None


def _salary_period(value: object) -> SalaryPeriod | None:
    normalized = (_clean_text(value) or "").casefold()
    return next(
        (
            period
            for marker, period in _SALARY_PERIODS.items()
            if marker in normalized
        ),
        None,
    )


def _salary_summary(
    minimum: float | None,
    maximum: float | None,
    period: SalaryPeriod | None,
) -> str | None:
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None and minimum != maximum:
        summary = f"{minimum:g} - {maximum:g}"
    else:
        summary = f"{minimum if minimum is not None else maximum:g}"
    if period:
        summary = f"{summary} per {period.casefold()}"
    return summary


def _parse_datetime(value: object) -> datetime | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)
