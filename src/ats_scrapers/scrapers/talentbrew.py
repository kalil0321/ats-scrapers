"""Radancy TalentBrew career sites.

TalentBrew is the career-site product large employers put in front of
their ATS, on their own domain (``jobs.citi.com``,
``careers.unitedhealthgroup.com``, ``jobs.aus.com``). Every site exposes
the same two public resources:

1. A search endpoint that returns the result list as an HTML fragment
   wrapped in JSON::

       GET https://{host}/search-jobs/results?CurrentPage=N&RecordsPerPage=500&...

   The fragment carries ``data-total-results`` / ``data-total-pages``
   and one ``<a href="/job/{city}/{title}/{org}/{id}" data-job-id>``
   per posting. The endpoint only answers with the full parameter set
   the site's own search form sends, so the scraper sends it too.

2. A detail page per posting with a schema.org ``JobPosting`` JSON-LD
   block: description, posting date, employer, structured address,
   employment type and the ATS requisition id.

A search stops returning rows after 10,000 postings, so on the few
sites above that size the walk ends there and a warning is logged with
the announced total. The scraper fails closed when a page inside the
walk holds no usable posting, or when the walk returns far fewer
postings than the site announced (the announced total counts a posting
once per location, so a complete walk can be somewhat short).

Descriptions need one request per posting, so the pipeline runs this
scraper with ``defer_descriptions_to_cache``: listings come from the
search endpoint and :meth:`get_description` is only called for postings
that are not in the description cache yet.

``company_slug`` is the career-site host or URL.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
RESULT_CAP = 10_000  # a search returns no rows past this many postings
# The announced total counts a posting once per location while the list
# shows it once, so a complete walk can hold fewer rows than announced.
MIN_COVERAGE = 0.6
DETAIL_CONCURRENCY = 4
MAX_DESCRIPTION_LEN = 25_000

# The parameter set the site's own search form sends. The endpoint
# answers with an empty shell when the module names are missing.
_SEARCH_PARAMS = {
    "ActiveFacetID": "0",
    "RecordsPerPage": str(PAGE_SIZE),
    "Distance": "50",
    "RadiusUnitType": "0",
    "Keywords": "",
    "Location": "",
    "ShowRadius": "False",
    "IsPagination": "False",
    "CustomFacetName": "",
    "FacetTerm": "",
    "FacetType": "0",
    "SearchResultsModuleName": "Search Results",
    "SearchFiltersModuleName": "Search Filters",
    "SortCriteria": "0",
    "SortDirection": "0",
    "SearchType": "5",
    "PostalCode": "",
    "ResultsType": "0",
}

_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
# Some sites prefix the path with a locale: /en/job/..., /fr-fr/job/...
_JOB_PATH_RE = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/job/[^?#]+/\d+/(\d+)/?$", re.IGNORECASE)
_LD_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_TAG_RE = re.compile(
    r"<\s*(?:br|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*/?\s*>", re.IGNORECASE,
)
_LI_OPEN_RE = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n\s*\n+")

_EMPLOYMENT_TYPES = {
    "full time": "FULL_TIME", "full-time": "FULL_TIME", "full_time": "FULL_TIME",
    "part time": "PART_TIME", "part-time": "PART_TIME", "part_time": "PART_TIME",
    "contractor": "CONTRACT", "contract": "CONTRACT",
    "temporary": "TEMPORARY", "seasonal": "TEMPORARY",
    "intern": "INTERN", "internship": "INTERN",
}


@ScraperRegistry.register(ATSType.TALENTBREW)
class TalentBrewScraper(BaseScraper):
    """All public postings of one Radancy TalentBrew career site."""

    ats = ATSType.TALENTBREW
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "application/json, text/html;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
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
        self.host = _normalize_host(company_slug)
        self.base_url = f"https://{self.host}"
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        async with self.make_fetcher() as fetch:
            fragment = await self._page(fetch, 1)
            total, pages = _totals(fragment)
            if total == 0:
                return []
            rows_seen = self._absorb(fragment, jobs)
            # Some sites ignore RecordsPerPage and keep their own page size,
            # so follow the page count the site reports, not ours.
            for page in range(2, pages + 1):
                if rows_seen >= RESULT_CAP:
                    break
                rows = self._absorb(await self._page(fetch, page), jobs)
                rows_seen += rows
                if rows == 0:
                    # A page without a usable posting before the last one
                    # means the walk was cut.
                    raise ScraperError(
                        f"TalentBrew ({self.host}) returned an empty page "
                        f"{page} of {pages}"
                    )

            expected = min(total, RESULT_CAP)
            if rows_seen < math.ceil(expected * MIN_COVERAGE):
                raise ScraperError(
                    f"TalentBrew ({self.host}) returned {rows_seen} of "
                    f"{expected} expected postings"
                )
            if total > RESULT_CAP:
                logger.warning(
                    "TalentBrew (%s) announces %d postings; the search stops "
                    "at %d, so %d were read",
                    self.host, total, RESULT_CAP, rows_seen,
                )
            if not self.include_descriptions:
                return list(jobs.values())

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            await asyncio.gather(
                *(self._enrich(fetch, semaphore, job) for job in jobs.values())
            )
        return list(jobs.values())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich(fetch, asyncio.Semaphore(1), copy)
            return copy.description

        return self._run_sync(run())

    async def _page(self, fetch: Fetcher, page: int) -> BeautifulSoup:
        payload = await fetch.get_json(
            f"{self.base_url}/search-jobs/results",
            params={**_SEARCH_PARAMS, "CurrentPage": str(page)},
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, str):
            raise ScraperError(
                f"TalentBrew ({self.host}) returned an unexpected payload on page {page}"
            )
        return _parse_html(results)

    def _absorb(self, fragment: BeautifulSoup, jobs: dict[str, Job]) -> int:
        """Add a page's postings to ``jobs``.

        Returns how many rows of the page parsed into a posting, so a page
        of rows that lost their id or title counts as empty.
        """
        rows = 0
        seen_on_page: set[str] = set()
        for link in fragment.select("a[data-job-id][href]"):
            href = str(link.get("href") or "")
            match = _JOB_PATH_RE.match(href)
            job_id = str(link.get("data-job-id") or "").strip()
            if not match or not job_id or match.group(1) != job_id:
                continue
            if job_id in seen_on_page:
                continue
            seen_on_page.add(job_id)
            title = _link_title(link)
            if not title:
                continue
            rows += 1
            ats_id = f"{self.host}:{job_id}"
            jobs.setdefault(
                ats_id,
                Job(
                    url=f"{self.base_url}{href}",
                    title=title,
                    company=self.company_name or self.host,
                    ats_type=ATSType.TALENTBREW,
                    ats_id=ats_id,
                    location=_link_location(link),
                    fetched_at=datetime.now(UTC),
                ),
            )
        return rows

    async def _enrich(
        self, fetch: Fetcher, semaphore: asyncio.Semaphore, job: Job,
    ) -> None:
        """Fill ``job`` from its detail page. A page that cannot be read
        leaves the listing row as it is."""
        async with semaphore:
            try:
                page = await fetch.get_text(str(job.url))
            except ScraperError:
                return
        posting = _job_posting(page)
        if posting is None:
            return
        if description := _html_to_text(posting.get("description")):
            job.description = description[:MAX_DESCRIPTION_LEN]
        if posted := _parse_date(posting.get("datePosted")):
            job.posted_at = posted
        if not self.company_name:
            organization = posting.get("hiringOrganization")
            if isinstance(organization, dict) and (name := _text(organization.get("name"))):
                job.company = name
        location, country_iso = _posting_location(posting)
        if location:
            job.location = location
        if country_iso:
            job.country_iso = country_iso
        commitment = _text(posting.get("employmentType"))
        if commitment:
            job.commitment = commitment
            job.employment_type = _EMPLOYMENT_TYPES.get(commitment.lower())
        if requisition := _identifier(posting.get("identifier")):
            job.requisition_id = requisition


def _normalize_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("TalentBrew needs a career-site host or URL")
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or parsed.username or parsed.port:
            raise ValueError(f"TalentBrew URL must be a plain https:// URL, got {value!r}")
        host = parsed.hostname or ""
    else:
        host = raw.split("/", 1)[0]
    host = host.lower().rstrip(".")
    if not _HOST_RE.match(host):
        raise ValueError(f"TalentBrew host is not a valid domain name: {value!r}")
    return host


def _parse_html(markup: str) -> BeautifulSoup:
    from bs4 import BeautifulSoup

    return BeautifulSoup(markup, "html.parser")


def _totals(fragment: BeautifulSoup) -> tuple[int, int]:
    holder = fragment.select_one("[data-total-results]")
    if holder is None:
        raise ScraperError("TalentBrew result list has no data-total-results")
    try:
        total = int(str(holder.get("data-total-results")))
        pages = int(str(holder.get("data-total-pages") or 0))
    except ValueError as exc:
        raise ScraperError("TalentBrew result totals are not numeric") from exc
    if total < 0 or pages < 0:
        raise ScraperError("TalentBrew result totals are negative")
    return total, max(pages, 1 if total else 0)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _link_title(link: Tag) -> str | None:
    heading = link.find(["h2", "h3", "h4"])
    return _text((heading or link).get_text(" ", strip=True))


def _link_location(link: Tag) -> str | None:
    """Location shown with a result row, inside the link or next to it."""
    scopes = [link]
    row = link.find_parent("li")
    if row is not None:
        scopes.append(row)
    for scope in scopes:
        node = scope.find(class_=re.compile(r"job-location"))
        if node is not None and (text := _text(node.get_text(" ", strip=True))):
            return text
    return None


def _identifier(value: object) -> str | None:
    """A JSON-LD ``identifier``: plain text, a number or a PropertyValue."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value)


def _is_job_posting(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    kind = entry.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "JobPosting" in kinds


def _job_posting(page: str) -> dict[str, Any] | None:
    for block in _LD_JSON_RE.findall(page):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in list(entries):
            graph = entry.get("@graph") if isinstance(entry, dict) else None
            if isinstance(graph, list):
                entries.extend(graph)
        for entry in entries:
            if _is_job_posting(entry):
                return entry
    return None


def _posting_location(posting: dict[str, Any]) -> tuple[str | None, str | None]:
    places = posting.get("jobLocation")
    if isinstance(places, dict):
        places = [places]
    if not isinstance(places, list):
        return None, None
    labels: list[str] = []
    countries: set[str] = set()
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [
            _text(address.get("addressLocality")),
            _text(address.get("addressRegion")),
            _text(country),
        ]
        label = ", ".join(part for part in parts if part)
        if label and label not in labels:
            labels.append(label)
        code = _text(country)
        if code and len(code) == 2 and code.isalpha():
            countries.add(code.upper())
    location = " | ".join(labels[:5]) or None
    return location, next(iter(countries)) if len(countries) == 1 else None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # JSON-LD descriptions are HTML that was escaped once more.
    text = html.unescape(value)
    text = _LI_OPEN_RE.sub("\n- ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
    return text or None
