"""Oracle Taleo Enterprise Edition career sections.

Taleo Enterprise is the large-employer edition of Taleo. (The small-business
edition, on ``*.tbe.taleo.net``, is a different product with different pages
and is handled by ``TaleoScraper``.)

A customer has a zone host, ``{zone}.taleo.net``, and one or more career
sections under it:

    https://{zone}.taleo.net/careersection/{section}/jobsearch.ftl

The search page carries the section's portal number and loads its results
from the endpoint its own search form calls:

    POST https://{zone}.taleo.net/careersection/rest/jobboard/searchjobs
         ?lang=en&portal={portal}

which answers one page of postings and the paging data:

    {"requisitionList": [{"jobId": "314420", "contestNo": "26001GK",
                          "column": ["Title", "[\\"US-SD-Aurora\\"]",
                                     "Nov 12, 2026"],
                          "linkedColumn": 0, "locationsColumns": [1]}],
     "pagingData": {"currentPageNo": 1, "pageSize": 25, "totalCount": 50}}

``totalCount`` is not a count of the postings the pages hold. On many
sections it is larger (682 announced for 258 listed on one, 38 for 15 on
another, whatever the sort order), and the gap is not at the end of the
list: a page can hold fewer than ``pageSize`` postings, or none, with more
postings on the pages after it, and a requisition can come back on more
than one page. The walk therefore reads every one of the
``ceil(totalCount / pageSize)`` pages, never stops at a short or empty page,
keeps each requisition once, and does not compare the number of postings
with the total.

``column`` follows the section's own layout, so only the positions the
payload names are trusted: ``linkedColumn`` is the title and
``locationsColumns`` are locations (each a JSON list of place names). A
remaining column that reads as a date is taken as the posting date.

The description is on the detail page (``jobdetail.ftl?job={contestNo}``),
inside the hidden ``initialHistory`` field: values separated by ``!|!``,
URL-encoded, with rich-text values marked by a ``!*!`` prefix.

``company_slug`` is ``{zone}.taleo.net/{section}`` or any URL of the section.
The same requisition is often listed in several sections of a zone, so
``ats_id`` is ``{zone}:{contestNo}``: unique across zones, equal across the
sections of one zone.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote, unquote, urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 25
# 2,000 pages is 50,000 postings: past this the walk raises instead of
# going on.
MAX_PAGES = 2_000
# At least this share of the rows listed must parse into a job.
MIN_COVERAGE = 0.9
DETAIL_CONCURRENCY = 6
MAX_DESCRIPTION_CHARS = 25_000

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.taleo\.net$")
_SECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_SECTION_PATH_RE = re.compile(r"^/careersection/([^/]+)(?:/|$)")
_PORTAL_RE = re.compile(r"\bportal=(\d{3,})")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HISTORY_INPUT_RE = re.compile(
    r"<input\b[^>]*\binitialHistory\b[^>]*>", re.IGNORECASE
)
_VALUE_RE = re.compile(r'\bvalue="([^"]*)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(?:p|div|li|ul|ol|h[1-6]|tr)>|<br\s*/?>", re.IGNORECASE)
_ESCAPE_RE = re.compile(r"\\(.)")

_FIELD_SEPARATOR = "!|!"
_RICH_TEXT_PREFIX = "!*!"

_DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
)

# The filters the search form sends when nothing is selected.
_BASIC_FILTERS = ("POSTING_DATE", "LOCATION", "JOB_FIELD")
_ADVANCED_FILTERS = (
    "ORGANIZATION",
    "LOCATION",
    "JOB_FIELD",
    "JOB_NUMBER",
    "URGENT_JOB",
    "EMPLOYEE_STATUS",
    "STUDY_LEVEL",
    "WILL_TRAVEL",
    "JOB_SHIFT",
)


@ScraperRegistry.register(ATSType.TALEO_ENTERPRISE)
class TaleoEnterpriseScraper(BaseScraper):
    """All postings of one Taleo Enterprise career section."""

    ats = ATSType.TALEO_ENTERPRISE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        # Some zones answer a redirect with a compressed body that does not
        # decompress; plain responses avoid that.
        "Accept-Encoding": "identity",
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
        try:
            self.host, self.section = _parse_slug(company_slug)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc
        self.zone = self.host.split(".", 1)[0]
        self.section_url = f"https://{self.host}/careersection/{self.section}"
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            portal = await self._portal(fetch)
            jobs = await self._walk(fetch, portal)
            if self.include_descriptions and jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(
                    *(self._enrich(fetch, sem, job) for job in jobs)
                )
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich(fetch, asyncio.Semaphore(1), job)
            return job.description

        return self._run_sync(run())

    async def _portal(self, fetch: Fetcher) -> str:
        """The section's portal number, read from its search page."""
        page = await fetch.get_text(f"{self.section_url}/jobsearch.ftl?lang=en")
        match = _PORTAL_RE.search(page)
        if match:
            return match.group(1)
        title = _TITLE_RE.search(page)
        label = _collapse(html.unescape(title.group(1))) if title else ""
        if "unavailable" in label.casefold() or "not found" in label.casefold():
            raise CompanyNotFoundError(
                f"Taleo Enterprise ({self.company_slug}): career section is "
                f"not available ({label!r})"
            )
        raise ScraperError(
            f"Taleo Enterprise ({self.company_slug}): the search page has no "
            f"portal number (page title {label!r})"
        )

    async def _walk(self, fetch: Fetcher, portal: str) -> list[Job]:
        url = (
            f"https://{self.host}/careersection/rest/jobboard/searchjobs"
            f"?lang=en&portal={portal}"
        )
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": f"https://{self.host}",
            "Referer": f"{self.section_url}/jobsearch.ftl?lang=en",
            # Posting dates are formatted for the caller's time zone.
            "tz": "GMT+00:00",
            "tzname": "UTC",
        }
        jobs: dict[str, Job] = {}
        total = 0
        page_count = 1
        rows_seen = 0
        rows_read = 0
        page = 1
        while page <= page_count:
            payload = await fetch.post_json(
                url, json=_search_payload(page), headers=headers
            )
            rows, page_total, page_size = self._check_payload(payload, page)
            if page == 1:
                total = page_total
                page_count = max(1, math.ceil(total / page_size))
                if page_count > MAX_PAGES:
                    raise ScraperError(
                        f"Taleo Enterprise ({self.company_slug}) announces "
                        f"{total} postings on {page_count} pages (page guard "
                        f"{MAX_PAGES}); refusing to return a partial list"
                    )
            for row in rows:
                job = self._parse_row(row)
                if job is not None and job.ats_id:
                    rows_read += 1
                    jobs.setdefault(job.ats_id, job)
            rows_seen += len(rows)
            # No early exit on a short or empty page: the pages after it
            # can still hold postings (see the module docstring).
            page += 1
        # A requisition can come back on more than one page, so the rows
        # are counted, not the distinct postings.
        if rows_read < math.ceil(rows_seen * MIN_COVERAGE):
            raise ScraperError(
                f"Taleo Enterprise ({self.company_slug}) could read only "
                f"{rows_read} of the {rows_seen} postings listed"
            )
        return list(jobs.values())

    def _check_payload(
        self, payload: object, page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Validate one search response.

        Returns its rows, the announced total and the page size.
        """
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Taleo Enterprise ({self.company_slug}) page {page}: "
                f"expected a JSON object"
            )
        if payload.get("careerSectionUnAvailable") is True:
            raise CompanyNotFoundError(
                f"Taleo Enterprise ({self.company_slug}): career section is "
                f"not available"
            )
        rows = payload.get("requisitionList")
        paging = payload.get("pagingData")
        total = paging.get("totalCount") if isinstance(paging, dict) else None
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
        ):
            raise ScraperError(
                f"Taleo Enterprise ({self.company_slug}) page {page}: "
                f"response has no posting list or total"
            )
        if any(not isinstance(row, dict) for row in rows):
            raise ScraperError(
                f"Taleo Enterprise ({self.company_slug}) page {page}: "
                f"a posting is not a JSON object"
            )
        size = paging.get("pageSize")
        page_size = (
            size
            if isinstance(size, int) and not isinstance(size, bool) and size > 0
            else PAGE_SIZE
        )
        return rows, total, page_size

    def _parse_row(self, row: dict[str, Any]) -> Job | None:
        contest = _text(row.get("contestNo"))
        key = contest or _text(row.get("jobId"))
        columns = row.get("column")
        if not key or not isinstance(columns, list) or not columns:
            return None
        linked = row.get("linkedColumn")
        title_index = (
            linked
            if isinstance(linked, int)
            and not isinstance(linked, bool)
            and 0 <= linked < len(columns)
            else 0
        )
        title = _text(columns[title_index])
        if not title:
            return None

        location_indexes = [
            index
            for index in row.get("locationsColumns") or []
            if isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(columns)
        ]
        places: list[str] = []
        for index in location_indexes:
            for place in _places(columns[index]):
                if place not in places:
                    places.append(place)

        posted_at = None
        for index, value in enumerate(columns):
            if index == title_index or index in location_indexes:
                continue
            posted_at = _parse_date(value)
            if posted_at is not None:
                break

        return Job(
            url=f"{self.section_url}/jobdetail.ftl?job={quote(key, safe='')}&lang=en",
            title=title,
            company=self.company_name or self.zone,
            ats_type=ATSType.TALEO_ENTERPRISE,
            ats_id=f"{self.zone}:{key}",
            requisition_id=contest,
            location="; ".join(places) or None,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw={
                "jobId": _text(row.get("jobId")),
                "contestNo": contest,
                "column": [value for value in columns if isinstance(value, str)],
            },
        )

    async def _enrich(
        self, fetch: Fetcher, sem: asyncio.Semaphore, job: Job
    ) -> None:
        # Best-effort: a detail page that fails must not drop the posting.
        async with sem:
            try:
                page = await fetch.get_text(str(job.url))
            except ScraperError:
                return
        description = _description(page)
        if description and not job.description:
            job.description = description


def _parse_slug(value: str) -> tuple[str, str]:
    """``(host, section)`` from ``zone.taleo.net/section`` or a section URL."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(
            "Taleo Enterprise needs '{zone}.taleo.net/{section}' or a "
            "career-section URL"
        )
    if "://" in raw:
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError(
                f"Taleo Enterprise URL must be a plain https:// URL, got {value!r}"
            )
        host = parsed.hostname or ""
        match = _SECTION_PATH_RE.match(parsed.path)
        section = match.group(1) if match else ""
    else:
        host, _, section = raw.partition("/")
        section = section.strip("/")
    host = host.lower().rstrip(".")
    if not _HOST_RE.match(host):
        raise ValueError(
            f"Taleo Enterprise host must be '{{zone}}.taleo.net', got {value!r}"
        )
    if not _SECTION_RE.match(section) or section == "rest":
        raise ValueError(
            f"Taleo Enterprise career section is missing or invalid in {value!r}"
        )
    return host, section


def _search_payload(page: int) -> dict[str, Any]:
    """The body the search form posts with no keyword and no filter."""

    def empty(names: tuple[str, ...]) -> dict[str, Any]:
        return {
            "searchFilterSelections": [
                {"id": name, "selectedValues": []} for name in names
            ]
        }

    return {
        "multilineEnabled": False,
        "sortingSelection": {
            "sortBySelectionParam": "3",
            "ascendingSortingOrder": "false",
        },
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
        "filterSelectionParam": empty(_BASIC_FILTERS),
        "advancedSearchFiltersSelectionParam": empty(_ADVANCED_FILTERS),
        "pageNo": page,
    }


def _text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        cleaned = _collapse(html.unescape(value))
        return cleaned or None
    return None


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _places(value: object) -> list[str]:
    """Place names of a location column: a JSON list, or plain text."""
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = value
    items = decoded if isinstance(decoded, list) else [decoded]
    places: list[str] = []
    for item in items:
        place = _text(item)
        if place:
            places.append(place)
    return places


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = _collapse(value)
    if not cleaned or len(cleaned) > 30:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _description(page: str) -> str | None:
    """The posting text from a detail page's ``initialHistory`` field."""
    tag = _HISTORY_INPUT_RE.search(page)
    if not tag:
        return None
    value = _VALUE_RE.search(tag.group(0))
    if not value:
        return None
    blocks: list[str] = []
    for field in html.unescape(value.group(1)).split(_FIELD_SEPARATOR):
        decoded = unquote(field)
        if not decoded.startswith(_RICH_TEXT_PREFIX):
            continue
        text = _plain_text(decoded[len(_RICH_TEXT_PREFIX):])
        # The page repeats each rich-text value.
        if text and text not in blocks:
            blocks.append(text)
    description = "\n\n".join(blocks)[:MAX_DESCRIPTION_CHARS].strip()
    return description or None


def _plain_text(fragment: str) -> str:
    # Field values escape separators with a backslash (``\:``, ``\,``).
    unescaped = _ESCAPE_RE.sub(r"\1", fragment)
    with_breaks = _BLOCK_END_RE.sub("\n", unescaped)
    text = html.unescape(_TAG_RE.sub(" ", with_breaks))
    lines = [_collapse(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
