"""HRMOS Recruiting scraper.

Public HRMOS employer portals expose active jobs as server-rendered HTML:

    GET https://hrmos.co/pages/{tenant}/jobs

Each page contains up to 100 postings with titles, substantial descriptions,
locations, and classification tags. Larger employers use ``?page=N``.
No account, API key, browser, JavaScript rendering, or per-job detail request
is required.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

BASE_URL = "https://hrmos.co/pages/{tenant}/jobs"
PAGE_SIZE = 100
_COUNT_RE = re.compile(r"全\s*([0-9,]+)\s*件中\s*([0-9,]+)\s*件")
_COMPANY_SUFFIX_RE = re.compile(r"\s+(?:すべての)?求人一覧$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REMOTE_RE = re.compile(r"(?:full[ -]?remote|remote|フルリモート|完全在宅)", re.I)
_JAPAN_LOCATION_RE = re.compile(r"(?:日本|Japan|北海道|東京都|京都府|大阪府|.{2,3}県)")
_EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "正社員": "FULL_TIME",
    "契約社員": "CONTRACT",
    "業務委託": "CONTRACT",
    "派遣社員": "TEMPORARY",
    "アルバイト": "PART_TIME",
    "パート": "PART_TIME",
    "アルバイト・パート": "PART_TIME",
    "インターン": "INTERN",
    "インターンシップ": "INTERN",
}


@ScraperRegistry.register(ATSType.HRMOS)
class HrmosScraper(BaseScraper):
    """Scrape one public HRMOS Recruiting tenant."""

    ats = ATSType.HRMOS

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = require_host_label(
            company_slug,
            provider="HrmosScraper",
        )

    async def afetch(self) -> list[Job]:
        url = BASE_URL.format(tenant=self.company_slug)
        async with self.make_fetcher() as fetch:
            first_html = await fetch.get_text(url)
            first = self._parse_page(first_html)
            jobs = list(first.jobs)
            for page in range(2, math.ceil(first.total / PAGE_SIZE) + 1):
                html = await fetch.get_text(url, params={"page": page})
                parsed = self._parse_page(html)
                if parsed.total != first.total or parsed.company != first.company:
                    raise ScraperError(
                        f"HRMOS ({self.company_slug}) changed while paginating"
                    )
                jobs.extend(parsed.jobs)

        job_ids = [job.ats_id for job in jobs]
        if len(job_ids) != len(set(job_ids)):
            raise ScraperError(
                f"HRMOS ({self.company_slug}) returned duplicate job IDs"
            )
        if len(jobs) != first.total:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) expected {first.total} jobs, "
                f"received {len(jobs)}"
            )
        return jobs

    def _parse_page(self, html: str) -> _Page:
        soup = BeautifulSoup(html, "html.parser")
        company_node = soup.select_one(".sg-corporate-name")
        count_node = soup.select_one(".pg-count")
        if company_node is None or count_node is None:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) returned an unrecognized careers page"
            )

        company = _COMPANY_SUFFIX_RE.sub(
            "",
            company_node.get_text(" ", strip=True),
        ).strip()
        count_match = _COUNT_RE.search(count_node.get_text(" ", strip=True))
        if not company or count_match is None:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) omitted page metadata"
            )
        total = int(count_match.group(1).replace(",", ""))
        displayed = int(count_match.group(2).replace(",", ""))
        company_options = _company_options(soup)

        jobs = [
            self._parse_card(card, company, company_options)
            for card in soup.select(".pg-list-cassette")
        ]
        if len(jobs) != displayed:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) advertised {displayed} jobs "
                f"on a page but exposed {len(jobs)}"
            )
        return _Page(company=company, total=total, jobs=jobs)

    def _parse_card(
        self,
        card: Tag,
        default_company: str,
        company_options: set[str],
    ) -> Job:
        link = card.select_one('a[href*="/jobs/"]')
        title_node = card.select_one("h2")
        if link is None or title_node is None:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) returned a malformed job card"
            )
        title = title_node.get_text(" ", strip=True)
        job_id, job_url = _trusted_job_url(
            link.get("href"),
            tenant=self.company_slug,
        )
        if not title:
            raise ScraperError(
                f"HRMOS ({self.company_slug}) job {job_id} omitted its title"
            )

        tag_nodes = card.select(".sg-tags li")
        tags = [
            tag
            for node in tag_nodes
            if (tag := node.get_text(" ", strip=True))
        ]
        company = next(
            (tag for tag in tags if tag in company_options),
            default_company,
        )
        location_node = card.select_one(".sg-tag-location")
        location = (
            location_node.get_text(" ", strip=True)
            if location_node is not None
            else ""
        ) or None
        commitment = next(
            (tag for tag in tags if tag in _EMPLOYMENT_TYPES),
            None,
        )
        description_node = card.select_one(".pg-list-cassette-body")
        description = (
            description_node.get_text("\n", strip=True)[:25_000]
            if self.include_descriptions and description_node is not None
            else None
        )
        remote_text = " ".join(part for part in (title, location) if part)
        country_iso = "JP" if location and _JAPAN_LOCATION_RE.search(location) else None
        raw_tags = [
            tag
            for tag in tags
            if tag != location and tag != company and tag != commitment
        ]

        return Job(
            url=job_url,
            title=title,
            company=company,
            ats_type=ATSType.HRMOS,
            ats_id=f"{self.company_slug}:{job_id}",
            location=location,
            country_iso=country_iso,
            region="Asia" if country_iso == "JP" else None,
            is_remote=True if _REMOTE_RE.search(remote_text) else None,
            employment_type=_EMPLOYMENT_TYPES.get(commitment or ""),
            commitment=commitment,
            description=description,
            fetched_at=datetime.now(UTC),
            apply_url=f"{job_url}/apply",
            language="ja",
            raw={"tags": raw_tags} if raw_tags else None,
        )


class _Page:
    __slots__ = ("company", "jobs", "total")

    def __init__(self, *, company: str, total: int, jobs: list[Job]) -> None:
        self.company = company
        self.total = total
        self.jobs = jobs


def _trusted_job_url(value: object, *, tenant: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ScraperError(f"HRMOS ({tenant}) job omitted its URL")
    parsed = urlparse(urljoin("https://hrmos.co", value.strip()))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ScraperError(f"HRMOS ({tenant}) returned an untrusted job URL") from exc
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hrmos.co"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or len(segments) != 4
        or segments[:3] != ["pages", tenant, "jobs"]
        or not _JOB_ID_RE.fullmatch(segments[3])
    ):
        raise ScraperError(f"HRMOS ({tenant}) returned an untrusted job URL")
    job_id = segments[3]
    return job_id, f"https://hrmos.co/pages/{tenant}/jobs/{job_id}"


def _company_options(soup: BeautifulSoup) -> set[str]:
    options: set[str] = set()
    for heading in soup.select("h3.jobCategory-title"):
        if heading.get_text(" ", strip=True) != "会社名" or heading.parent is None:
            continue
        options.update(
            text
            for node in heading.parent.select(".sg-filter-button")
            if (text := node.get_text(" ", strip=True))
        )
    return options
