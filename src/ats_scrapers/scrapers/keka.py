"""Keka public career portal scraper.

Keka employer portals expose a credential-free HTML bootstrap and two JSON
endpoints:

    GET https://{tenant}.keka.com/careers[/{portal}]
    GET /careers/api/organization/{portal}/careerportalinfo
    GET /careers/api/embedjobs/{portal}/active/{identifier}

The active-jobs response includes full descriptions, structured locations,
publication dates, employment type, salary metadata, experience, skills, and
employer requisition numbers.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.keka\.com$",
    re.IGNORECASE,
)
_PORTAL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(
    r"/ats/documents/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/careerportal/",
    re.IGNORECASE,
)
_SCRIPT_IDENTIFIER_RE = re.compile(
    r"/careers/api/embedjobs/js/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_EMPLOYMENT_TYPES: dict[int, EmploymentType] = {
    1: "PART_TIME",
    2: "FULL_TIME",
}
_SALARY_PERIODS: dict[int, SalaryPeriod] = {
    1: "HOUR",
    3: "MONTH",
    4: "YEAR",
}
_SALARY_PERIOD_LABELS = {
    0: "Not Available",
    1: "Hourly",
    2: "Bi Weekly",
    3: "Monthly",
    4: "Annual",
}
_COUNTRY_BY_NAME = {
    "brazil": "BR",
    "canada": "CA",
    "colombia": "CO",
    "egypt": "EG",
    "gabon": "GA",
    "germany": "DE",
    "hong kong": "HK",
    "india": "IN",
    "indonesia": "ID",
    "japan": "JP",
    "kazakhstan": "KZ",
    "kenya": "KE",
    "kuwait": "KW",
    "malaysia": "MY",
    "mauritius": "MU",
    "mexico": "MX",
    "nepal": "NP",
    "netherlands": "NL",
    "oman": "OM",
    "pakistan": "PK",
    "philippines": "PH",
    "qatar": "QA",
    "saudi arabia": "SA",
    "singapore": "SG",
    "spain": "ES",
    "sri lanka": "LK",
    "taiwan": "TW",
    "thailand": "TH",
    "united arab emirates": "AE",
    "united kingdom": "GB",
    "united states": "US",
}
_REGION_BY_COUNTRY = {
    "AE": "Asia",
    "BR": "South America",
    "CA": "North America",
    "CO": "South America",
    "DE": "Europe",
    "EG": "Africa",
    "ES": "Europe",
    "GA": "Africa",
    "GB": "Europe",
    "HK": "Asia",
    "ID": "Asia",
    "IN": "Asia",
    "JP": "Asia",
    "KE": "Africa",
    "KW": "Asia",
    "KZ": "Asia",
    "LK": "Asia",
    "MU": "Africa",
    "MX": "North America",
    "MY": "Asia",
    "NL": "Europe",
    "NP": "Asia",
    "OM": "Asia",
    "PH": "Asia",
    "PK": "Asia",
    "QA": "Asia",
    "SA": "Asia",
    "SG": "Asia",
    "TH": "Asia",
    "TW": "Asia",
    "US": "North America",
}


@ScraperRegistry.register(ATSType.KEKA)
class KekaScraper(BaseScraper):
    """Scrape one public Keka employer portal."""

    ats = ATSType.KEKA
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "text/html,application/json",
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
        self.portal_url, self.host, self.portal = _normalize_portal_url(
            company_slug
        )
        self.company_slug = self.portal_url
        self.company_name = _string(company_name)

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            html = await fetch.get_text(self.portal_url)
            identifier = _extract_identifier(html, self.host)
            api_root = f"https://{self.host}/careers/api"
            jobs_request = fetch.get_json(
                f"{api_root}/embedjobs/{self.portal}/active/{identifier}"
            )
            if self.company_name:
                jobs_payload = await jobs_request
                company_payload: object = {}
            else:
                jobs_payload, company_payload = await asyncio.gather(
                    jobs_request,
                    _optional_json(
                        fetch,
                        f"{api_root}/organization/{self.portal}/careerportalinfo",
                    ),
                )
        company = _company_name(
            company_payload,
            explicit=self.company_name,
            fallback=self.host.split(".", 1)[0],
            host=self.host,
        )
        return self._parse_jobs(jobs_payload, company, identifier)

    def _parse_jobs(
        self,
        payload: object,
        company: str,
        identifier: str,
    ) -> list[Job]:
        if not isinstance(payload, list):
            raise ScraperError(
                f"Keka ({self.host}/{self.portal}) active jobs response "
                "was not a list"
            )
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ScraperError(
                    f"Keka ({self.host}/{self.portal}) row {index} "
                    "was not an object"
                )
            job = self._parse_job(item, company, identifier)
            if job.ats_id in seen_ids:
                raise ScraperError(
                    f"Keka ({self.host}/{self.portal}) returned duplicate "
                    f"job ID {job.ats_id}"
                )
            seen_ids.add(job.ats_id or "")
            jobs.append(job)
        return jobs

    def _parse_job(
        self,
        item: dict[str, Any],
        company: str,
        identifier: str,
    ) -> Job:
        job_id = _job_id(item.get("id"))
        title = _required_string(item, "title", host=self.host)
        locations = item.get("jobLocations")
        location, country_iso = _locations(locations)
        job_type = item.get("jobType")
        employment_type = (
            _EMPLOYMENT_TYPES.get(job_type)
            if isinstance(job_type, int) and not isinstance(job_type, bool)
            else None
        )
        salary_range = item.get("salaryRange")
        salary = _salary(salary_range, item.get("salaryRangeFormat"))
        description = _html_to_text(item.get("description"))
        experience_raw = _string(item.get("experience"))
        department = _string(item.get("departmentName"))
        requisition_id = _string(item.get("jobNumber"))
        job_url = f"{self.portal_url}/jobdetails/{job_id}"
        raw = {
            key: value
            for key, value in {
                "portal": self.portal,
                "portal_identifier": identifier,
                "department_identifier": item.get("departmentIdentifier"),
                "experience": experience_raw,
                "published_since_days": item.get("publishedSinceDays"),
                "salary_period": (
                    _SALARY_PERIOD_LABELS.get(salary_range.get("salaryPeriod"))
                    if isinstance(salary_range, dict)
                    else None
                ),
                "skills": item.get("skillNames"),
            }.items()
            if value not in (None, "", [])
        }
        return Job(
            url=job_url,
            title=title,
            company=company,
            ats_type=ATSType.KEKA,
            ats_id=f"{self.host}:{self.portal}:{job_id}",
            location=location,
            country_iso=country_iso,
            region=_REGION_BY_COUNTRY.get(country_iso),
            salary_min=salary["minimum"],
            salary_max=salary["maximum"],
            salary_currency=salary["currency"],
            salary_period=salary["period"],
            salary_summary=salary["summary"],
            employment_type=employment_type,
            department=department,
            requisition_id=requisition_id,
            commitment=(
                "Full Time"
                if job_type == 2
                else "Part Time"
                if job_type == 1
                else None
            ),
            experience=_minimum_experience(experience_raw),
            description=(
                description[:25_000]
                if self.include_descriptions and description
                else None
            ),
            posted_at=_parse_datetime(item.get("publishedOn")),
            fetched_at=datetime.now(UTC),
            apply_url=job_url,
            raw=raw or None,
        )


def _normalize_portal_url(value: str) -> tuple[str, str, str]:
    cleaned = value.strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    try:
        parsed = urlparse(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ScraperError(
            "KekaScraper requires a public *.keka.com/careers URL"
        ) from exc
    host = (parsed.hostname or "").lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    valid_path = (
        len(segments) in {1, 2}
        and segments[0].casefold() == "careers"
        and (
            len(segments) == 1
            or _PORTAL_RE.fullmatch(segments[1]) is not None
        )
    )
    if (
        parsed.scheme != "https"
        or _HOST_RE.fullmatch(host) is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise ScraperError(
            "KekaScraper requires a public *.keka.com/careers URL"
        )
    portal = segments[1].casefold() if len(segments) == 2 else "default"
    portal_url = (
        f"https://{host}/careers"
        if portal == "default"
        else f"https://{host}/careers/{portal}"
    )
    return portal_url, host, portal


def _extract_identifier(html: str, host: str) -> str:
    match = _IDENTIFIER_RE.search(html) or _SCRIPT_IDENTIFIER_RE.search(html)
    if match is None:
        raise ScraperError(
            f"Keka ({host}) careers page omitted its portal identifier"
        )
    return match.group("id").casefold()


def _company_name(
    payload: object,
    *,
    explicit: str | None,
    fallback: str,
    host: str,
) -> str:
    if not isinstance(payload, dict):
        raise ScraperError(f"Keka ({host}) company response was not an object")
    return (
        explicit
        or _string(payload.get("shortName"))
        or _string(payload.get("name"))
        or fallback
    )


def _job_id(value: object) -> str:
    if isinstance(value, bool):
        raise ScraperError("Keka job omitted a valid numeric ID")
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    raise ScraperError("Keka job omitted a valid numeric ID")


def _required_string(
    item: dict[str, Any],
    key: str,
    *,
    host: str,
) -> str:
    value = _string(item.get(key))
    if value is None:
        raise ScraperError(f"Keka ({host}) job omitted {key}")
    return value


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _locations(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, list):
        return None, None
    displays: list[str] = []
    countries: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        country_name = _string(item.get("countryName"))
        country_iso = _location_country_iso(
            country_name,
            _string(item.get("countryCode")),
        )
        if country_iso:
            countries.add(country_iso)
        pieces: list[str] = []
        for candidate in (
            item.get("city"),
            item.get("state"),
            country_name,
        ):
            text = _string(candidate)
            if (
                text
                and text.casefold() not in {"na", "n/a", "none"}
                and text not in pieces
            ):
                pieces.append(text)
        display = ", ".join(pieces) or _string(item.get("name"))
        if display and display not in displays:
            displays.append(display)
    country_iso = next(iter(countries)) if len(countries) == 1 else None
    return "; ".join(displays) or None, country_iso


def _salary(
    value: object,
    summary_value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        return _empty_salary(_string(summary_value))
    raw_period = value.get("salaryPeriod")
    if raw_period == 0:
        return _empty_salary()
    minimum = _number(value.get("minimum"))
    maximum = _number(value.get("maximum"))
    summary = _string(summary_value)
    salary_is_present = any(
        candidate is not None
        for candidate in (minimum, maximum, summary)
    )
    currency = (
        _string(value.get("currency")) if salary_is_present else None
    )
    if currency and len(currency) != 3:
        currency = None
    period = (
        _SALARY_PERIODS.get(raw_period)
        if isinstance(raw_period, int) and not isinstance(raw_period, bool)
        else None
    )
    return {
        "minimum": minimum,
        "maximum": maximum,
        "currency": currency.upper() if currency else None,
        "period": period,
        "summary": summary,
    }


def _location_country_iso(
    country_name: str | None,
    raw_code: str | None,
) -> str | None:
    if country_name:
        mapped = _COUNTRY_BY_NAME.get(country_name.casefold())
        if mapped:
            return mapped
    candidate = (raw_code or "").upper()
    return candidate if re.fullmatch(r"[A-Z]{2}", candidate) else None


def _empty_salary(summary: str | None = None) -> dict[str, object]:
    return {
        "minimum": None,
        "maximum": None,
        "currency": None,
        "period": None,
        "summary": summary,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


async def _optional_json(fetch: Any, url: str) -> object:
    try:
        return await fetch.get_json(url)
    except Exception:
        return {}


def _minimum_experience(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.match(r"^\s*(\d{1,2})(?:\D|$)", value)
    if match is None:
        return None
    years = int(match.group(1))
    return years if years <= 80 else None


def _parse_datetime(value: object) -> datetime | None:
    text = _string(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _html_to_text(value: object) -> str | None:
    text = _string(value)
    if text is None:
        return None
    cleaned = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    return cleaned or None
