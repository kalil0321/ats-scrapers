"""PageUp public careers scraper."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

from pydantic import HttpUrl, ValidationError

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

    from ats_scrapers.fetch import Fetcher

BASE_URL = "https://careers.pageuppeople.com"
PAGE_SIZE = 1000
DETAIL_CONCURRENCY = 8
_TENANT_RE = re.compile(
    r"^(?P<client>\d+)/(?P<channel>[A-Za-z0-9-]+)/"
    r"(?P<locale>[A-Za-z]{2}(?:-[A-Za-z]{2})?)$"
)
_JOB_ID_RE = re.compile(r"/job/(\d+)(?:/|$)", re.IGNORECASE)
_COUNT_RE = re.compile(r"\d[\d,]*")
_NO_JOBS_MARKERS = (
    "no jobs matched",
    "no jobs available",
    "there are no jobs available",
    "no current opportunities",
)
_EMPLOYMENT_PATTERNS: tuple[tuple[str, EmploymentType], ...] = (
    ("intern", "INTERN"),
    ("casual", "TEMPORARY"),
    ("temporary", "TEMPORARY"),
    ("fixed term", "CONTRACT"),
    ("fixed-term", "CONTRACT"),
    ("contract", "CONTRACT"),
    ("part time", "PART_TIME"),
    ("part-time", "PART_TIME"),
    ("full time", "FULL_TIME"),
    ("full-time", "FULL_TIME"),
    ("ongoing", "FULL_TIME"),
    ("permanent", "FULL_TIME"),
)
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)
logger = logging.getLogger(__name__)


@ScraperRegistry.register(ATSType.PAGEUP)
class PageUpScraper(BaseScraper):
    """Scrape one public ``careers.pageuppeople.com`` tenant."""

    ats = ATSType.PAGEUP
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
        match = _TENANT_RE.fullmatch(self.tenant_path)
        if match is None:
            raise ValueError(f"Invalid PageUp tenant path: {company_slug!r}")
        self.language = match.group("locale").split("-", 1)[0].lower()
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else f"PageUp {match.group('client')}"
        )
        self.base_url = f"{BASE_URL}/{self.tenant_path}"

    async def afetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        visited_urls: set[str] = set()
        expected_total: int | None = None
        next_url: str | None = (
            f"{self.base_url}/listing/?page=1&page-items={PAGE_SIZE}"
        )

        async with self.make_fetcher() as fetch:
            while next_url is not None:
                if next_url in visited_urls:
                    raise ScraperError(
                        f"PageUp ({self.tenant_path}) repeated a pagination URL"
                    )
                visited_urls.add(next_url)
                listing_html = await fetch.get_text(next_url)
                page_jobs, next_url, remaining = self._parse_listing(listing_html)
                if not jobs and remaining is not None:
                    expected_total = len(page_jobs) + remaining
                for job in page_jobs:
                    if not job.ats_id or job.ats_id in seen_ids:
                        raise ScraperError(
                            f"PageUp returned duplicate job id {job.ats_id!r}"
                        )
                    seen_ids.add(job.ats_id)
                    jobs.append(job)

            if expected_total is not None and len(jobs) != expected_total:
                raise ScraperError(
                    "PageUp catalogue ended before the reported total "
                    f"({len(jobs)}/{expected_total})"
                )
            if not self.include_descriptions or not jobs:
                return jobs

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            enriched = await asyncio.gather(
                *(self._enrich_detail(fetch, semaphore, job) for job in jobs)
            )
        completed = [job for job in enriched if job is not None]
        if jobs and not completed:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) lost every listed job "
                "during detail validation"
            )
        return completed

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                enriched = await self._enrich_detail(
                    fetch,
                    asyncio.Semaphore(1),
                    copy,
                )
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
                "Dropping PageUp job %s after detail failure: %s",
                job.ats_id,
                exc,
            )
            return None
        if response.status_code in {404, 410}:
            return None
        if "jobnotfound=true" in response.text.lower():
            return None
        try:
            _apply_detail(job, response.text)
        except ScraperError as exc:
            logger.warning(
                "Dropping PageUp job %s after detail parse failure: %s",
                job.ats_id,
                exc,
            )
            return None
        if not job.description:
            logger.warning(
                "Dropping PageUp job %s because its detail page omitted "
                "a description",
                job.ats_id,
            )
            return None
        return job

    def _parse_listing(
        self,
        html_text: str,
    ) -> tuple[list[Job], str | None, int | None]:
        soup = _parse_html(html_text)
        container = soup.select_one("#search-results-content")
        if container is None:
            page_text = _clean_text(soup.get_text(" ", strip=True)).lower()
            if any(marker in page_text for marker in _NO_JOBS_MARKERS):
                return [], None, None
            raise ScraperError(
                f"PageUp ({self.tenant_path}) omitted search results"
            )

        anchors = container.select('a.job-link[href*="/job/"]')
        if not anchors:
            page_text = _clean_text(container.get_text(" ", strip=True)).lower()
            if any(marker in page_text for marker in _NO_JOBS_MARKERS):
                return [], None, None
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned an empty job list"
            )

        anchors_by_id: dict[str, Tag] = {}
        for anchor in anchors:
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            match = _JOB_ID_RE.search(href)
            title = _clean_text(anchor.get_text(" ", strip=True))
            if match is None or not title:
                continue
            job_id = match.group(1)
            existing = anchors_by_id.get(job_id)
            if existing is None:
                anchors_by_id[job_id] = anchor
                continue
            existing_title = _clean_text(existing.get_text(" ", strip=True))
            if _is_generic_link_text(existing_title):
                anchors_by_id[job_id] = anchor
            elif not _is_generic_link_text(title):
                raise ScraperError(
                    f"PageUp returned duplicate job id {job_id!r}"
                )

        jobs: list[Job] = []
        for job_id, anchor in anchors_by_id.items():
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            title = _clean_text(anchor.get_text(" ", strip=True))
            if _is_generic_link_text(title):
                continue
            detail_url = self._tenant_url(href, expected_segment="job")
            row = anchor.find_parent("tr") or anchor.find_parent(class_="card")
            location_node = row.select_one(".location") if row else None
            location = (
                _clean_text(location_node.get_text(" ", strip=True))
                if location_node is not None
                else ""
            )
            work_type_node = row.select_one(".work-type") if row else None
            work_type = (
                _clean_text(work_type_node.get_text(" ", strip=True))
                if work_type_node is not None
                else ""
            )
            department_node = row.select_one(".department") if row else None
            department = (
                _clean_text(department_node.get_text(" ", strip=True))
                if department_node is not None
                else ""
            )
            raw: dict[str, Any] = {}
            if row is not None:
                closing = row.select_one(".close-date time")
                if closing is not None:
                    closes_at = closing.get("datetime")
                    if isinstance(closes_at, str) and closes_at:
                        raw["closes_at"] = closes_at
                summary = row.find_next_sibling("tr", class_="summary")
                if summary is not None:
                    summary_text = _clean_text(summary.get_text(" ", strip=True))
                    if summary_text:
                        raw["listing_summary"] = summary_text
            jobs.append(
                Job(
                    url=HttpUrl(detail_url),
                    title=title,
                    company=self.company_name,
                    ats_type=ATSType.PAGEUP,
                    ats_id=f"{self.tenant_path}:{job_id}",
                    requisition_id=job_id,
                    location=location or None,
                    is_remote=(
                        True if location and "remote" in location.lower() else None
                    ),
                    employment_type=_employment_type(work_type),
                    department=department or None,
                    commitment=work_type or None,
                    language=self.language,
                    fetched_at=datetime.now(UTC),
                    raw=raw or None,
                )
            )

        if not jobs:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned no usable jobs"
            )

        results_wrapper = container.find_parent(id="search-results")
        more_link = (
            results_wrapper.select_one("a.more-link[href]")
            if results_wrapper is not None
            else None
        )
        if more_link is None:
            return jobs, None, None
        href = more_link.get("href")
        if not isinstance(href, str) or not href:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned an invalid next page"
            )
        count_node = more_link.select_one(".count")
        count_match = _COUNT_RE.search(
            count_node.get_text(" ", strip=True) if count_node else ""
        )
        if count_match is None:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) omitted remaining-job count"
            )
        remaining = int(count_match.group(0).replace(",", ""))
        if remaining <= 0:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned invalid remaining count"
            )
        return jobs, self._tenant_url(
            href,
            expected_segment="listing",
        ), remaining

    def _tenant_url(self, href: str, *, expected_segment: str) -> str:
        try:
            resolved = urljoin(f"{BASE_URL}/", href)
            parsed = urlparse(resolved)
            port = parsed.port
        except ValueError as exc:
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned an unsafe "
                f"{expected_segment} URL"
            ) from exc
        expected_prefix = (
            f"/{self.tenant_path}/{expected_segment}"
        ).casefold()
        normalized_path = parsed.path.casefold().rstrip("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "careers.pageuppeople.com"
            or port not in (None, 443)
            or not (
                normalized_path == expected_prefix
                or normalized_path.startswith(f"{expected_prefix}/")
            )
        ):
            raise ScraperError(
                f"PageUp ({self.tenant_path}) returned an unsafe "
                f"{expected_segment} URL"
            )
        return resolved


def _normalize_tenant_path(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("PageUp tenant path cannot be empty")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "careers.pageuppeople.com"
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "PageUp tenant URL must use https://careers.pageuppeople.com"
            )
        raw = parsed.path.strip("/")
    segments = [segment for segment in raw.split("/") if segment]
    if segments and segments[0].lower() == "mob":
        segments.pop(0)
    while segments and segments[-1].lower() in {
        "listing",
        "search",
        "job",
    }:
        segments.pop()
    normalized = "/".join(segments)
    if _TENANT_RE.fullmatch(normalized) is None:
        raise ValueError(f"Invalid PageUp tenant path: {value!r}")
    return normalized


def _apply_detail(job: Job, html_text: str) -> None:
    soup = _parse_html(html_text)
    container = soup.select_one("#job-content")
    if container is None:
        raise ScraperError(f"PageUp detail page missing content for {job.ats_id}")

    metadata = _extract_metadata(container)
    if requisition_id := _first_metadata(
        metadata,
        "job no",
        "job no.",
        "job number",
        "position number",
    ):
        job.requisition_id = requisition_id
    if location := _first_metadata(metadata, "location", "locations"):
        job.location = location
        job.is_remote = True if "remote" in location.lower() else None
    if work_type := _first_metadata(
        metadata,
        "work type",
        "employment type",
        "position type",
        "appointment type",
    ):
        job.employment_type = _employment_type(work_type)
        job.commitment = work_type
    if commitment := _first_metadata(
        metadata,
        "duration",
        "job duration",
    ):
        job.commitment = (
            f"{job.commitment}; {commitment}"
            if job.commitment
            else commitment
        )
    if department := _first_metadata(
        metadata,
        "department",
        "category",
        "categories",
        "division/organization",
        "faculty / portfolio",
    ):
        job.department = department
    if salary := _first_metadata(
        metadata,
        "salary",
        "salary range",
        "salary/wage range or lump sum",
        "remuneration",
        "level / salary",
    ):
        job.salary_summary = salary
    if posted := _first_metadata(
        metadata,
        "open date",
        "date advertised",
        "posting date",
        "posted",
    ):
        job.posted_at = _parse_date(posted)

    apply_link = container.select_one("a.apply-link[href]")
    if apply_link is not None:
        href = apply_link.get("href")
        if isinstance(href, str) and href:
            try:
                apply_url = urljoin(str(job.url), href)
                parsed_apply = urlparse(apply_url)
                if (
                    parsed_apply.scheme != "https"
                    or parsed_apply.hostname is None
                    or not (
                        parsed_apply.hostname == "careers.pageuppeople.com"
                        or parsed_apply.hostname.endswith(".pageuppeople.com")
                    )
                    or parsed_apply.username is not None
                    or parsed_apply.password is not None
                    or parsed_apply.port not in (None, 443)
                ):
                    raise ValueError("unsafe PageUp apply URL")
                job.apply_url = HttpUrl(apply_url)
            except (ValidationError, ValueError):
                logger.warning(
                    "Ignoring invalid PageUp apply URL for job %s",
                    job.ats_id,
                )

    description_soup = _parse_html(str(container))
    description = description_soup.select_one("#job-content")
    if description is None:
        return
    for node in description.select(
        "script, style, noscript, .social-share-kit, "
        "a.apply-link, a.back-link, a.employee-referral-link"
    ):
        node.decompose()
    job.description = _clean_text(
        description.get_text("\n", strip=True)
    )[:25_000] or None


def _extract_metadata(container: Tag) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for label_node in container.select("strong"):
        label = _clean_text(label_node.get_text(" ", strip=True))
        normalized = label.lower().strip(" :.")
        if not normalized or len(normalized) > 40:
            continue
        parent = label_node.parent
        if parent is None:
            continue
        full_text = _clean_text(parent.get_text(" ", strip=True))
        if not full_text.lower().startswith(label.lower()):
            continue
        value = full_text[len(label) :].lstrip(" :.-")
        if value and value not in values.setdefault(normalized, []):
            values[normalized].append(value)
    return values


def _first_metadata(
    metadata: dict[str, list[str]],
    *keys: str,
) -> str | None:
    for key in keys:
        values = metadata.get(key)
        if values:
            return "; ".join(values)
    return None


def _employment_type(value: str) -> EmploymentType | None:
    normalized = value.lower().replace("_", " ")
    for needle, mapped in _EMPLOYMENT_PATTERNS:
        if needle in normalized:
            return mapped
    return None


def _is_generic_link_text(value: str) -> bool:
    return value.lower().strip(" .:") in {
        "apply",
        "apply now",
        "details",
        "learn more",
        "view",
        "view job",
    }


def _parse_date(value: str) -> datetime | None:
    candidate = value.strip()
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    for date_format in _DATE_FORMATS:
        with contextlib.suppress(ValueError):
            return datetime.strptime(candidate, date_format).replace(tzinfo=UTC)
    return None


def _parse_html(html_text: str) -> BeautifulSoup:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            "PageUp scraper requires beautifulsoup4; "
            "install `ats-scrapers[scrapers]`"
        ) from exc
    return BeautifulSoup(html_text, "html.parser")


def _clean_text(value: str) -> str:
    unescaped = html.unescape(value)
    lines = [
        re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        for line in unescaped.splitlines()
    ]
    return "\n".join(line for line in lines if line).strip()
