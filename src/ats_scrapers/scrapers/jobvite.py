"""Jobvite public careers scraper."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

from pydantic import HttpUrl, ValidationError

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from ats_scrapers.fetch import Fetcher

BASE_URL = "https://jobs.jobvite.com"
DETAIL_CONCURRENCY = 8
_TENANT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_JOB_ID_RE = re.compile(r"/job/([A-Za-z0-9_-]+)(?:[/?#]|$)")
_PAGE_TEXT_RE = re.compile(r"^\s*(\d+)-(\d+)\s+of\s+(\d+)\s*$", re.IGNORECASE)
_GENERIC_LOCATION_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)
logger = logging.getLogger(__name__)

_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "FULLTIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "PARTTIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
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


@ScraperRegistry.register(ATSType.JOBVITE)
class JobviteScraper(BaseScraper):
    """Scrape a public ``jobs.jobvite.com`` tenant."""

    ats = ATSType.JOBVITE
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
        self.tenant_path = _normalize_tenant_path(company_slug)
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else self.tenant_path.rsplit("/", 1)[-1]
        )
        self.base_url = f"{BASE_URL}/{self.tenant_path}"

    async def afetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        page = 0
        expected_start = 1
        reported_total: int | None = None

        async with self.make_fetcher() as fetch:
            while True:
                listing_url = f"{self.base_url}/search?p={page}"
                listing_html = await fetch.get_text(listing_url)
                page_jobs, start, end, total = self._parse_listing(listing_html)
                if total == 0:
                    if page == 0 and not jobs:
                        return []
                    raise ScraperError(
                        f"Jobvite ({self.tenant_path}) catalogue became empty "
                        "during pagination"
                    )
                if reported_total is None:
                    reported_total = total
                elif total != reported_total:
                    raise ScraperError(
                        "Jobvite total changed during pagination "
                        f"({reported_total} to {total})"
                    )
                if start != expected_start:
                    raise ScraperError(
                        "Jobvite pagination gap "
                        f"(expected row {expected_start}, got {start})"
                    )
                if len(page_jobs) != end - start + 1:
                    raise ScraperError(
                        "Jobvite listing count did not match pagination range "
                        f"({len(page_jobs)} rows for {start}-{end})"
                    )
                for job in page_jobs:
                    if not job.ats_id or job.ats_id in seen_ids:
                        raise ScraperError(
                            f"Jobvite returned duplicate job id {job.ats_id!r}"
                        )
                    seen_ids.add(job.ats_id)
                    jobs.append(job)
                if end >= total:
                    break
                expected_start = end + 1
                page += 1

            if reported_total is None or len(jobs) != reported_total:
                raise ScraperError(
                    "Jobvite catalogue ended before the reported total "
                    f"({len(jobs)}/{reported_total})"
                )
            if not self.include_descriptions:
                return jobs

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            enriched = await asyncio.gather(
                *(self._enrich_detail(fetch, semaphore, job) for job in jobs)
            )
        completed = [job for job in enriched if job is not None]
        if jobs and not completed:
            raise ScraperError(
                f"Jobvite ({self.tenant_path}) lost every listed job "
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
                enriched = await self._enrich_detail(fetch, semaphore, copy)
            return enriched.description if enriched is not None else None

        return self._run_sync(run())

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
                "Retaining Jobvite job %s without detail metadata after "
                "detail failure: %s",
                job.ats_id,
                exc,
            )
            return job
        if response.status_code in {404, 410}:
            return None
        _apply_detail(job, response.text)
        if not job.description:
            logger.warning(
                "Dropping Jobvite job %s because its detail page omitted "
                "a description",
                job.ats_id,
            )
            return None
        return job

    def _parse_listing(
        self,
        html_text: str,
    ) -> tuple[list[Job], int, int, int]:
        soup = _parse_html(html_text)
        container = soup.select_one(".jv-job-list")
        if container is None:
            raise ScraperError(
                f"Jobvite ({self.tenant_path}) omitted the job-list container"
            )
        anchors = container.select('a[href*="/job/"]')
        if not anchors:
            lowered = _clean_text(container.get_text(" ", strip=True)).lower()
            if any(
                marker in lowered
                for marker in ("no jobs", "no positions", "no openings")
            ):
                return [], 0, 0, 0
            raise ScraperError(
                f"Jobvite ({self.tenant_path}) returned an empty job list"
            )

        pagination = soup.select_one(".jv-pagination-text")
        match = _PAGE_TEXT_RE.match(
            pagination.get_text(" ", strip=True) if pagination else ""
        )
        if match is None:
            raise ScraperError(
                f"Jobvite ({self.tenant_path}) omitted valid pagination metadata"
            )
        start, end, total = (int(value) for value in match.groups())
        if not (1 <= start <= end <= total):
            raise ScraperError(
                f"Jobvite ({self.tenant_path}) returned invalid pagination range"
            )

        jobs: list[Job] = []
        for anchor in anchors:
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            id_match = _JOB_ID_RE.search(href)
            name_cell = anchor.find_parent(class_="jv-job-list-name")
            title_node = (
                anchor.select_one(".jv-job-list-name")
                or name_cell
                or anchor
            )
            if id_match is None:
                continue
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not title:
                continue
            detail_url = urljoin(f"{BASE_URL}/", href)
            parsed_url = urlparse(detail_url)
            accepted_tenant_paths = [self.tenant_path]
            if self.tenant_path.startswith("careers/"):
                accepted_tenant_paths.append(self.tenant_path.split("/", 1)[1])
            if (
                not _is_trusted_jobvite_url(detail_url)
                or not any(
                    parsed_url.path.startswith(f"/{tenant_path}/job/")
                    for tenant_path in accepted_tenant_paths
                )
            ):
                raise ScraperError(
                    f"Jobvite ({self.tenant_path}) returned an unsafe job URL"
                )
            row = anchor.find_parent("tr")
            location_node = anchor.select_one(".jv-job-list-location")
            if location_node is None and row is not None:
                location_node = row.select_one(".jv-job-list-location")
            location = (
                _clean_text(location_node.get_text(" ", strip=True))
                if location_node is not None
                else ""
            )
            jobs.append(
                Job(
                    url=HttpUrl(detail_url),
                    title=title,
                    company=self.company_name,
                    ats_type=ATSType.JOBVITE,
                    ats_id=id_match.group(1),
                    location=location or None,
                    is_remote=(
                        True if location and "remote" in location.lower() else None
                    ),
                    fetched_at=datetime.now(UTC),
                )
            )
        return jobs, start, end, total


def _normalize_tenant_path(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Jobvite tenant path cannot be empty")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if not _is_trusted_jobvite_url(raw):
            raise ValueError("Jobvite tenant URL must use https://jobs.jobvite.com")
        raw = parsed.path.strip("/")
    segments = [segment for segment in raw.split("/") if segment]
    while segments and segments[-1] in {"search", "jobs", "viewall"}:
        segments.pop()
    valid_shape = (
        (len(segments) == 1 and segments[0] != "careers")
        or (len(segments) == 2 and segments[0] == "careers")
    )
    if not valid_shape or not all(
        _TENANT_SEGMENT_RE.fullmatch(segment) for segment in segments
    ):
        raise ValueError(f"Invalid Jobvite tenant path: {value!r}")
    return "/".join(segments)


def _apply_detail(job: Job, html_text: str) -> None:
    soup = _parse_html(html_text)
    posting = _find_job_posting(soup)
    if posting is not None:
        description = posting.get("description")
        if isinstance(description, str) and description.strip():
            job.description = _html_to_text(description)[:25_000] or None

        date_posted = posting.get("datePosted")
        if isinstance(date_posted, str):
            job.posted_at = _parse_date(date_posted)

        organization = posting.get("hiringOrganization")
        if isinstance(organization, dict):
            name = organization.get("name")
            if isinstance(name, str) and name.strip():
                job.company = name.strip()

        identifier = posting.get("identifier")
        if isinstance(identifier, str) and identifier.strip():
            job.requisition_id = identifier.strip()

        industry = posting.get("industry")
        if isinstance(industry, str) and industry.strip():
            job.department = _clean_text(industry)

        employment_type = _employment_type(posting.get("employmentType"))
        if employment_type is not None:
            job.employment_type = employment_type
        structured_remote = _is_remote_job_location_type(
            posting.get("jobLocationType")
        )
        if structured_remote:
            job.is_remote = True

        structured_location = _location_from_jsonld(posting.get("jobLocation"))
        if structured_location and (
            not job.location or _GENERIC_LOCATION_RE.match(job.location)
        ):
            job.location = structured_location
            job.is_remote = (
                True
                if structured_remote or "remote" in structured_location.lower()
                else None
            )

        _apply_salary(job, posting.get("baseSalary"))

    department, location = _detail_meta(soup)
    if department and not job.department:
        job.department = department
    if location and (not job.location or _GENERIC_LOCATION_RE.match(job.location)):
        job.location = location
        job.is_remote = (
            True
            if job.is_remote or "remote" in location.lower()
            else None
        )

    apply_link = soup.select_one("a.jv-button-apply[href]")
    if apply_link is not None:
        href = apply_link.get("href")
        if isinstance(href, str) and href:
            try:
                apply_url = urljoin(str(job.url), href)
                if not _is_trusted_jobvite_url(apply_url):
                    raise ValueError("untrusted Jobvite apply URL")
                job.apply_url = HttpUrl(apply_url)
            except (ValidationError, ValueError):
                logger.warning(
                    "Ignoring invalid Jobvite apply URL for job %s",
                    job.ats_id,
                )

    if not job.description:
        container = soup.select_one(".jv-job-detail-description")
        if container is not None:
            heading = container.find(
                lambda tag: tag.name in {"h2", "h3"}
                and _clean_text(tag.get_text(" ", strip=True)).lower()
                == "description"
            )
            if heading is not None:
                heading.decompose()
            job.description = _clean_text(
                container.get_text("\n", strip=True)
            )[:25_000] or None


def _parse_html(html_text: str) -> BeautifulSoup:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            "Jobvite scraper requires beautifulsoup4; "
            "install `ats-scrapers[scrapers]`"
        ) from exc
    return BeautifulSoup(html_text, "html.parser")


def _is_trusted_jobvite_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "jobs.jobvite.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _find_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw)
            for item in _jsonld_items(payload):
                if item.get("@type") == "JobPosting":
                    return item
    return None


def _jsonld_items(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    graph = value.get("@graph")
    if isinstance(graph, list):
        return [item for item in graph if isinstance(item, dict)]
    return [value]


def _detail_meta(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    meta = soup.select_one(".jv-job-detail-meta")
    if meta is None:
        return None, None
    values = [
        _clean_text(value)
        for value in meta.stripped_strings
        if _clean_text(value) and _clean_text(value) != "|"
    ]
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return values[0], ", ".join(values[1:])


def _location_from_jsonld(value: object) -> str | None:
    entries = value if isinstance(value, list) else [value]
    locations: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if not isinstance(address, dict):
            continue
        parts: list[str] = []
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            item = address.get(key)
            if isinstance(item, dict):
                item = item.get("name")
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        location = ", ".join(dict.fromkeys(parts))
        if location and location not in locations:
            locations.append(location)
    return "; ".join(locations) or None


def _is_remote_job_location_type(value: object) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(
        isinstance(item, str)
        and item.strip().upper() in {"TELECOMMUTE", "REMOTE"}
        for item in values
    )


def _employment_type(value: object) -> EmploymentType | None:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, str):
            continue
        normalized = re.sub(r"[^A-Z0-9]+", "_", item.upper()).strip("_")
        if mapped := _EMPLOYMENT_TYPE_MAP.get(normalized):
            return mapped
    return None


def _apply_salary(job: Job, value: object) -> None:
    if not isinstance(value, dict):
        return
    currency = value.get("currency")
    salary_value = value.get("value")
    if not isinstance(salary_value, dict):
        return
    minimum = _to_float(salary_value.get("minValue") or salary_value.get("value"))
    maximum = _to_float(salary_value.get("maxValue") or salary_value.get("value"))
    unit = salary_value.get("unitText")
    period = (
        _SALARY_PERIOD_MAP.get(str(unit).strip().upper())
        if unit is not None
        else None
    )
    if minimum is not None:
        job.salary_min = minimum
    if maximum is not None:
        job.salary_max = maximum
    if isinstance(currency, str) and len(currency.strip()) == 3:
        job.salary_currency = currency.strip().upper()
    if period is not None:
        job.salary_period = period


def _parse_date(value: str) -> datetime | None:
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _html_to_text(value: str) -> str:
    return _clean_text(_parse_html(value).get_text("\n", strip=True))


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(value)).strip()


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    with contextlib.suppress(TypeError, ValueError):
        return float(value)  # type: ignore[arg-type]
    return None
