"""Paylocity public recruiting scraper."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from ats_scrapers.fetch import Fetcher

BASE_URL = "https://recruiting.paylocity.com"
DETAIL_CONCURRENCY = 2
_PAGE_DATA_RE = re.compile(r"\bwindow\.pageData\s*=\s*")
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "FULLTIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "PARTTIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "SEASONAL": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}
_SALARY_PERIOD_MAP: dict[str, SalaryPeriod] = {
    "HOUR": "HOUR",
    "HOURLY": "HOUR",
    "DAY": "DAY",
    "DAILY": "DAY",
    "WEEK": "WEEK",
    "WEEKLY": "WEEK",
    "MONTH": "MONTH",
    "MONTHLY": "MONTH",
    "YEAR": "YEAR",
    "YEARLY": "YEAR",
    "ANNUAL": "YEAR",
    "ANNUALLY": "YEAR",
}
_COUNTRY_CODES = {
    "CAN": "CA",
    "CANADA": "CA",
    "MEX": "MX",
    "MEXICO": "MX",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "USA": "US",
}
logger = logging.getLogger(__name__)


@ScraperRegistry.register(ATSType.PAYLOCITY)
class PaylocityScraper(BaseScraper):
    """Scrape one public Paylocity external-job module."""

    ats = ATSType.PAYLOCITY
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "text/html,application/xhtml+xml",
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
        self.board_id = _normalize_board_id(company_slug)
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )
        self.listing_url = (
            f"{BASE_URL}/Recruiting/Jobs/All/{self.board_id}"
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            listing_html = await fetch.get_text(self.listing_url)
            payload = _extract_page_data(listing_html)
            jobs = self._parse_listing(payload)
            if not self.include_descriptions or not jobs:
                return jobs

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            enriched = await asyncio.gather(
                *(self._enrich_detail(fetch, semaphore, job) for job in jobs)
            )

        completed = [job for job in enriched if job is not None]
        if jobs and not completed:
            raise ScraperError(
                f"Paylocity ({self.board_id}) lost every listed job "
                "during detail validation"
            )
        return completed

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                semaphore = asyncio.Semaphore(1)
                enriched = await self._enrich_detail(
                    fetch,
                    semaphore,
                    copy,
                )
            return enriched.description if enriched is not None else None

        return self._run_sync(run())

    def _parse_listing(self, payload: dict[str, Any]) -> list[Job]:
        rows = payload.get("Jobs")
        if not isinstance(rows, list):
            raise ScraperError(
                f"Paylocity ({self.board_id}) omitted the Jobs list"
            )
        module_title = _string(payload.get("ModuleTitle"))
        module_id = _string(payload.get("ModuleId"))
        fallback_company = self.company_name or (
            module_title
            if module_title
            and module_title.casefold() not in {
                "career opportunities",
                "employment opportunities",
                "job opportunities",
            }
            else self.board_id
        )

        jobs: list[Job] = []
        seen_ids: set[int] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ScraperError(
                    f"Paylocity listing row {index} was not an object"
                )
            is_internal = row.get("IsInternal")
            if not isinstance(is_internal, bool):
                raise ScraperError(
                    f"Paylocity row {index} omitted the external-job flag"
                )
            if is_internal:
                continue

            job_id = _job_id(row.get("JobId"))
            if job_id in seen_ids:
                raise ScraperError(
                    f"Paylocity returned duplicate job id {job_id}"
                )
            seen_ids.add(job_id)
            title = _required_string(row, "JobTitle")
            location_data = row.get("JobLocation")
            location = _listing_location(
                location_data,
                _string(row.get("LocationName")),
            )
            explicit_remote = row.get("IsRemote")
            is_remote = (
                explicit_remote
                if isinstance(explicit_remote, bool)
                else (
                    True
                    if location and "remote" in location.casefold()
                    else None
                )
            )
            raw = {
                key: value
                for key, value in {
                    "board_id": self.board_id,
                    "module_id": module_id,
                    "module_title": module_title,
                    "location_id": (
                        location_data.get("LocationId")
                        if isinstance(location_data, dict)
                        else None
                    ),
                    "indeed_remote_type": row.get("IndeedRemoteType"),
                }.items()
                if value not in (None, "")
            }

            jobs.append(Job(
                url=f"{BASE_URL}/Recruiting/Jobs/Details/{job_id}",
                title=title,
                company=fallback_company,
                ats_type=ATSType.PAYLOCITY,
                ats_id=f"{self.board_id}:{job_id}",
                location=location,
                country_iso=_listing_country(location_data),
                is_remote=is_remote,
                department=_string(row.get("HiringDepartment")),
                posted_at=_parse_date(row.get("PublishedDate")),
                fetched_at=datetime.now(tz=UTC),
                requisition_id=str(job_id),
                apply_url=f"{BASE_URL}/Recruiting/Jobs/Apply/{job_id}",
                raw=raw or None,
            ))
        return jobs

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        job: Job,
    ) -> Job | None:
        try:
            async with semaphore:
                response = await fetch.request(
                    "GET",
                    str(job.url),
                    handled={404, 410},
                )
        except ScraperError as exc:
            logger.warning(
                "Retaining Paylocity job %s without detail metadata after "
                "detail failure: %s",
                job.ats_id,
                exc,
            )
            return job
        if response.status_code in {404, 410}:
            return None
        try:
            _apply_detail(job, response.text)
        except ScraperError as exc:
            logger.warning(
                "Retaining Paylocity job %s without detail metadata after "
                "detail parsing failure: %s",
                job.ats_id,
                exc,
            )
        return job


def _normalize_board_id(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Paylocity board id cannot be empty")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid Paylocity URL port") from exc
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "recruiting.paylocity.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or len(segments) != 4
            or [segment.casefold() for segment in segments[:3]]
            != ["recruiting", "jobs", "all"]
        ):
            raise ValueError(
                "Paylocity URL must be an https recruiting job-list URL"
            )
        raw = segments[3]
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise ValueError(f"Invalid Paylocity board id: {value!r}") from exc


def _extract_page_data(html_text: str) -> dict[str, Any]:
    matches = list(_PAGE_DATA_RE.finditer(html_text))
    if len(matches) != 1:
        raise ScraperError(
            "Paylocity listing must contain exactly one window.pageData payload"
        )
    start = matches[0].end()
    try:
        payload, consumed = json.JSONDecoder().raw_decode(html_text[start:])
    except json.JSONDecodeError as exc:
        raise ScraperError(
            f"Paylocity listing pageData was not valid JSON: {exc}"
        ) from exc
    if not html_text[start + consumed :].lstrip().startswith(";"):
        raise ScraperError("Paylocity listing pageData was not terminated")
    if not isinstance(payload, dict):
        raise ScraperError("Paylocity listing pageData was not an object")
    return payload


def _apply_detail(job: Job, html_text: str) -> None:
    posting = _find_job_posting(html_text)
    if posting is None:
        description = _find_legacy_description(html_text)
        if description is None:
            raise ScraperError(
                f"Paylocity detail page for {job.ats_id} omitted job content"
            )
        job.description = description
        return
    description = posting.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ScraperError(
            f"Paylocity detail page for {job.ats_id} omitted a description"
        )
    job.description = description.strip()

    title = _string(posting.get("title"))
    if title:
        job.title = title
    organization = posting.get("hiringOrganization")
    if isinstance(organization, dict):
        company = _string(organization.get("name"))
        if company:
            job.company = company
    posted_at = _parse_date(posting.get("datePosted"))
    if posted_at is not None:
        job.posted_at = posted_at
    employment_type = _employment_type(posting.get("employmentType"))
    if employment_type is not None:
        job.employment_type = employment_type
    if _is_remote(posting.get("jobLocationType")):
        job.is_remote = True

    location, country_iso = _jsonld_location(posting.get("jobLocation"))
    if location:
        job.location = location
    if country_iso:
        job.country_iso = country_iso
    _apply_salary(job, posting.get("baseSalary"))


def _find_job_posting(html_text: str) -> dict[str, Any] | None:
    soup = _parse_html(html_text)
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw)
            for item in _jsonld_items(payload):
                item_type = item.get("@type")
                types = item_type if isinstance(item_type, list) else [item_type]
                if "JobPosting" in types:
                    return item
    return None


def _find_legacy_description(html_text: str) -> str | None:
    soup = _parse_html(html_text)
    sections: list[str] = []
    for header in soup.select(".job-preview-details .job-listing-header"):
        label = header.get_text(" ", strip=True)
        body = header.find_next_sibling("div")
        if not label or body is None or not body.get_text(" ", strip=True):
            continue
        content = body.decode_contents().strip()
        if content:
            sections.append(f"<p>{html.escape(label)}</p>{content}")
    return "".join(sections) or None


def _parse_html(html_text: str) -> BeautifulSoup:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            "Paylocity scraper requires beautifulsoup4; "
            "install `ats-scrapers[scrapers]`"
        ) from exc
    return BeautifulSoup(html_text, "html.parser")


def _jsonld_items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    graph = value.get("@graph")
    if isinstance(graph, list):
        return [item for item in graph if isinstance(item, dict)]
    return [value]


def _listing_location(value: object, fallback: str | None) -> str | None:
    if not isinstance(value, dict):
        return fallback
    parts = [
        _string(value.get("City")) or _string(value.get("Metro")),
        _string(value.get("State")),
        _country_code(value.get("Country")),
    ]
    joined = ", ".join(dict.fromkeys(part for part in parts if part))
    return joined or fallback


def _listing_country(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    return _country_code(value.get("Country"))


def _jsonld_location(value: object) -> tuple[str | None, str | None]:
    candidates = value if isinstance(value, list) else [value]
    locations: list[str] = []
    country_iso: str | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        address = candidate.get("address")
        if not isinstance(address, dict):
            continue
        country = _country_code(address.get("addressCountry"))
        parts = [
            _string(address.get("addressLocality")),
            _string(address.get("addressRegion")),
            country,
        ]
        location = ", ".join(dict.fromkeys(part for part in parts if part))
        if location and location not in locations:
            locations.append(location)
        country_iso = country_iso or country
    return "; ".join(locations) or None, country_iso


def _country_code(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    text = _string(value)
    if not text:
        return None
    upper = text.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return _COUNTRY_CODES.get(upper)


def _employment_type(value: object) -> EmploymentType | None:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, str):
            continue
        normalized = re.sub(r"[^A-Z0-9]+", "_", item.upper()).strip("_")
        if mapped := _EMPLOYMENT_TYPE_MAP.get(normalized):
            return mapped
    return None


def _is_remote(value: object) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(
        isinstance(item, str)
        and item.strip().upper() in {"REMOTE", "TELECOMMUTE"}
        for item in values
    )


def _apply_salary(job: Job, value: object) -> None:
    if not isinstance(value, dict):
        return
    salary_value = value.get("value")
    if not isinstance(salary_value, dict):
        return
    minimum = _to_float(
        salary_value.get("minValue") or salary_value.get("value")
    )
    maximum = _to_float(
        salary_value.get("maxValue") or salary_value.get("value")
    )
    currency = _string(value.get("currency"))
    unit = _string(salary_value.get("unitText"))
    if minimum is not None:
        job.salary_min = minimum
    if maximum is not None:
        job.salary_max = maximum
    if currency and len(currency) == 3:
        job.salary_currency = currency.upper()
    if unit and (period := _SALARY_PERIOD_MAP.get(unit.upper())):
        job.salary_period = period


def _parse_date(value: object) -> datetime | None:
    text = _string(value)
    if not text:
        return None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    with contextlib.suppress(TypeError, ValueError):
        return float(value)  # type: ignore[arg-type]
    return None


def _job_id(value: object) -> int:
    if isinstance(value, bool):
        raise ScraperError("Paylocity job id cannot be boolean")
    with contextlib.suppress(TypeError, ValueError):
        parsed = int(value)  # type: ignore[arg-type]
        if parsed > 0 and str(parsed) == str(value).strip():
            return parsed
    raise ScraperError(f"Invalid Paylocity job id: {value!r}")


def _required_string(item: dict[str, Any], key: str) -> str:
    value = _string(item.get(key))
    if not value:
        raise ScraperError(f"Paylocity row omitted {key}")
    return value


def _string(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None
