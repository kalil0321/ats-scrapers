"""Jibe career sites (iCIMS "Career Sites", formerly Jibe).

Large iCIMS customers put a branded career site in front of the ATS,
on their own domain (``careers.costco.com``, ``careers.ulta.com``). The
site is backed by a public JSON search API:

    GET https://{host}/api/jobs?page=N&limit=100&internal=false

Each response carries ``totalCount`` and a ``jobs`` list whose entries
wrap the posting under ``data``: requisition id, title, full HTML
description, structured location, employer name, posting date and the
iCIMS apply URL.

Two limits shape the walk:

* ``limit`` is capped at 100 (a larger value returns HTTP 422).
* Some sites stop paging at a depth of their own, and past it every
  page repeats the last one served: ``jobs.uhsinc.com`` and
  ``careers.costco.com`` serve 5,100 rows a query, while
  ``careers.ulta.com`` serves all of its 11,000. A walk ends at the
  first page that brings mostly rows it has already read.

A query cut short this way is read again sorted by title
(``sortBy=title``), once from each end: ``descending`` reverses the
title order whatever its value, though it does not change the default
order. The two ends cover a query of up to twice the cap. A larger
query, or one the two ends still leave short, is split by state, then
by category, and each part is read the same way. The state list comes
from the ``state`` facet when the site exposes it
(``filter.facetList.state``) and otherwise from the location list
(``filter.locations.all``); the ``state`` query parameter filters on it
either way. Categories are tried on a query whose states do not add up
to its total: postings without a state are reachable only that way.

The scraper fails closed: a walk that ends with fewer than 90% of the
announced rows raises instead of returning a partial board. Coverage is
counted on rows that parse into a posting: one requisition listed for
several locations or languages is several rows but a single posting.

``company_slug`` is the career-site host or URL.
"""

from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote, urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 100
TITLE_ORDERS: tuple[dict[str, str], ...] = (
    {"sortBy": "title"},
    {"sortBy": "title", "descending": "true"},
)
# The query parameters a query past the cap is split on, in order.
SPLITS = ("state", "categories")
# A finished walk must hold at least this share of the announced total.
MIN_COVERAGE = 0.9
MAX_DESCRIPTION_LEN = 25_000

_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_BLOCK_TAG_RE = re.compile(
    r"<\s*(?:br|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*/?\s*>", re.IGNORECASE,
)
_LI_OPEN_RE = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n\s*\n+")

_EMPLOYMENT_TYPES = {
    "full_time": "FULL_TIME", "full-time": "FULL_TIME", "full time": "FULL_TIME",
    "part_time": "PART_TIME", "part-time": "PART_TIME", "part time": "PART_TIME",
    "contractor": "CONTRACT", "contract": "CONTRACT",
    "temporary": "TEMPORARY", "temp": "TEMPORARY", "seasonal": "TEMPORARY",
    "intern": "INTERN", "internship": "INTERN",
}


@ScraperRegistry.register(ATSType.JIBE)
class JibeScraper(BaseScraper):
    """All public postings of one Jibe (iCIMS) career site."""

    ats = ATSType.JIBE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "application/json",
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
            first = await self._page(fetch, 1, {})
            total = _total(first)
            if total == 0:
                return []
            # One entry per valid row the API returned. A requisition listed
            # in several places or languages is several rows but one posting,
            # so coverage is measured on rows, not on postings.
            rows_seen = await self._exhaust(fetch, {}, first, jobs)

        if len(rows_seen) < math.ceil(total * MIN_COVERAGE):
            raise ScraperError(
                f"Jibe ({self.host}) returned {len(rows_seen)} of {total} "
                "announced rows"
            )
        return list(jobs.values())

    async def _exhaust(
        self,
        fetch: Fetcher,
        filters: dict[str, str],
        first: dict[str, Any],
        jobs: dict[str, Job],
    ) -> set[str]:
        """Read every row of one query; return the keys of the rows read."""
        rows: set[str] = set()
        cap, latest = await self._walk(fetch, filters, jobs, rows, first=first)
        # A posting closed during the read lowers the total; one opened
        # during it is left for the next run.
        total = min(_total(first), latest)
        if cap is None or len(rows) >= total:
            return rows
        if total <= 2 * cap:
            for order in TITLE_ORDERS:
                _, latest = await self._walk(
                    fetch, {**filters, **order}, jobs, rows, until=total,
                )
                total = min(total, latest)
                if len(rows) >= total:
                    return rows
        for name in SPLITS:
            if name in filters:
                continue
            values = _split_values(first, name)
            for value, _ in values:
                part = {**filters, name: value}
                rows |= await self._exhaust(fetch, part, await self._page(fetch, 1, part), jobs)
                if len(rows) >= total:
                    return rows
            if sum(count for _, count in values) >= total:
                break
        return rows

    async def _walk(
        self,
        fetch: Fetcher,
        filters: dict[str, str],
        jobs: dict[str, Job],
        rows: set[str],
        *,
        first: dict[str, Any] | None = None,
        until: int | None = None,
    ) -> tuple[int | None, int]:
        """Read one query page by page, until ``rows`` holds ``until`` keys.

        Returns the number of rows the site serves before its pages repeat
        (None when the walk ends without a repeat) and the total the last
        page announced.
        """
        payload = first if first is not None else await self._page(fetch, 1, filters)
        walked: set[str] = set()
        page = 1
        while True:
            total = _total(payload)
            keys = self._absorb(payload, jobs)
            rows |= keys
            # Past the cap a page repeats the last one served, give or take
            # a row the default order moves between two requests.
            if len(keys - walked) * 2 < len(keys):
                return (page - 1) * PAGE_SIZE, total
            walked |= keys
            if (
                len(payload["jobs"]) < PAGE_SIZE
                or page >= math.ceil(total / PAGE_SIZE)
                or (until is not None and len(rows) >= until)
            ):
                return None, total
            page += 1
            payload = await self._page(fetch, page, filters)

    async def _page(
        self,
        fetch: Fetcher,
        page: int,
        filters: dict[str, str],
    ) -> dict[str, Any]:
        params = {
            "page": str(page),
            "limit": str(PAGE_SIZE),
            "internal": "false",
            **filters,
        }
        url = f"{self.base_url}/api/jobs"
        response = await fetch.request("GET", url, params=params)
        # A site that left Jibe sends its API path to the new careers page.
        answered = urlparse(response.url) if response.url else None
        if (
            answered is not None
            and (answered.hostname, answered.path.rstrip("/")) != (self.host, "/api/jobs")
            and not response.text.lstrip().startswith(("{", "["))
        ):
            raise CompanyNotFoundError(
                f"Jibe site {self.base_url} sends its job API to {response.url}: "
                "it no longer runs on Jibe"
            )
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ScraperError(
                f"Jibe ({self.host}) returned an unexpected payload on page {page}"
            )
        return payload

    def _absorb(self, payload: dict[str, Any], jobs: dict[str, Job]) -> set[str]:
        """Add a page's postings to ``jobs``; return the keys of its rows.

        Only rows that parse into a posting have a key, so a feed whose rows
        lost their id or title fails the coverage check.
        """
        keys: set[str] = set()
        for row in payload["jobs"]:
            data = row.get("data") if isinstance(row, dict) else None
            if not isinstance(data, dict):
                raise ScraperError(f"Jibe ({self.host}) returned a posting without data")
            job = self._parse_job(data)
            if job is None or not job.ats_id:
                continue
            keys.add(_row_key(job.ats_id, data))
            # English first: the list is not ordered by language, so a
            # translated twin never replaces a row already kept.
            existing = jobs.get(job.ats_id)
            if existing is None or (
                (existing.language or "") != "en" and job.language == "en"
            ):
                jobs[job.ats_id] = job
        return keys

    def _parse_job(self, data: dict[str, Any]) -> Job | None:
        slug = _text(data.get("slug")) or _text(data.get("req_id"))
        title = _text(data.get("title"))
        if not slug or not title:
            return None
        language_tag = _text(data.get("language"))
        url = f"{self.base_url}/jobs/{quote(slug, safe='')}"
        if language_tag:
            url += f"?lang={quote(language_tag, safe='-')}"

        country_code = _text(data.get("country_code"))
        country_iso = (
            country_code.upper()
            if country_code and len(country_code) == 2 and country_code.isalpha()
            else None
        )
        location = _location(data)
        commitment = _text(data.get("employment_type"))
        apply_url = _text(data.get("apply_url"))
        if apply_url and apply_url.startswith("/"):
            apply_url = f"{self.base_url}{apply_url}"
        elif apply_url and not apply_url.startswith(("https://", "http://")):
            apply_url = None
        categories = [
            name
            for entry in (data.get("categories") or [])
            if isinstance(entry, dict) and (name := _text(entry.get("name")))
        ]

        raw: dict[str, Any] = {}
        for key in ("location_name", "street_address", "postal_code", "ats_code"):
            if value := _text(data.get(key)):
                raw[key] = value
        if len(categories) > 1:
            raw["categories"] = categories[:10]

        return Job(
            url=url,
            title=title,
            company=self.company_name or _text(data.get("hiring_organization")) or self.host,
            ats_type=ATSType.JIBE,
            # Requisition ids repeat across employers; the host makes them unique.
            ats_id=f"{self.host}:{slug}",
            location=location,
            country_iso=country_iso,
            is_remote=True if location and "remote" in location.lower() else None,
            department=categories[0] if categories else None,
            employment_type=_EMPLOYMENT_TYPES.get(commitment.lower()) if commitment else None,
            commitment=commitment,
            requisition_id=_text(data.get("req_id")),
            apply_url=apply_url if apply_url and apply_url != url else None,
            language=language_tag.split("-")[0].lower() if language_tag else None,
            description=_description(data) if self.include_descriptions else None,
            posted_at=_parse_datetime(data.get("posted_date")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _normalize_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Jibe needs a career-site host or URL")
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or parsed.username or parsed.port:
            raise ValueError(f"Jibe URL must be a plain https:// URL, got {value!r}")
        host = parsed.hostname or ""
    else:
        host = raw.split("/", 1)[0]
    host = host.lower().rstrip(".")
    if not _HOST_RE.match(host):
        raise ValueError(f"Jibe host is not a valid domain name: {value!r}")
    return host


def _total(payload: dict[str, Any]) -> int:
    total = payload.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ScraperError("Jibe response has no numeric totalCount")
    return total


def _row_key(ats_id: str, data: dict[str, Any]) -> str:
    """Identity of one listed row: the requisition plus where and in which
    language it is listed."""
    parts = (
        _text(data.get(key)) or ""
        for key in ("language", "location_name", "full_location", "city", "postal_code")
    )
    return "|".join((ats_id, *parts))


def _split_values(payload: dict[str, Any], name: str) -> list[tuple[str, int]]:
    """The values of query parameter ``name`` the response offers, with counts."""
    if name == "state":
        return _state_facet(payload) or _states_from_locations(payload)
    return _categories(payload)


def _state_facet(payload: dict[str, Any]) -> list[tuple[str, int]]:
    facets = payload.get("filter")
    facet_list = facets.get("facetList") if isinstance(facets, dict) else None
    states = facet_list.get("state") if isinstance(facet_list, dict) else None
    out: list[tuple[str, int]] = []
    for entry in states or []:
        if not isinstance(entry, dict):
            continue
        term, count = _text(entry.get("term")), entry.get("count")
        if term and isinstance(count, int) and not isinstance(count, bool) and count > 0:
            out.append((term, count))
    return out


def _states_from_locations(payload: dict[str, Any]) -> list[tuple[str, int]]:
    facets = payload.get("filter")
    locations = facets.get("locations") if isinstance(facets, dict) else None
    entries = locations.get("all") if isinstance(locations, dict) else None
    totals: dict[str, int] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        state, count = _text(entry.get("state")), entry.get("count")
        if state and isinstance(count, int) and not isinstance(count, bool) and count > 0:
            totals[state] = totals.get(state, 0) + count
    return sorted(totals.items())


def _categories(payload: dict[str, Any]) -> list[tuple[str, int]]:
    facets = payload.get("filter")
    categories = facets.get("categories") if isinstance(facets, dict) else None
    entries = categories.get("all") if isinstance(categories, dict) else None
    out: list[tuple[str, int]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name, count = _text(entry.get("category")), entry.get("numJobs")
        if name and isinstance(count, int) and not isinstance(count, bool) and count > 0:
            out.append((name, count))
    return out


def _text(value: object) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _location(data: dict[str, Any]) -> str | None:
    full = _text(data.get("full_location")) or _text(data.get("short_location"))
    country = _text(data.get("country"))
    if full:
        if country and country.lower() not in full.lower():
            return f"{full}, {country}"
        return full
    parts = [_text(data.get("city")), _text(data.get("state")), country]
    text = ", ".join(part for part in parts if part)
    return text or None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # The API writes offsets as ``+0000``; fromisoformat wants ``+00:00``.
    match = re.fullmatch(r"(.*[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)([+-]\d{2})(\d{2})", raw)
    if match:
        raw = f"{match.group(1)}{match.group(2)}:{match.group(3)}"
    elif raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _description(data: dict[str, Any]) -> str | None:
    sections = [
        (None, data.get("description")),
        ("Responsibilities", data.get("responsibilities")),
        ("Qualifications", data.get("qualifications")),
    ]
    parts: list[str] = []
    for heading, value in sections:
        text = _html_to_text(value)
        if not text:
            continue
        parts.append(f"{heading}\n{text}" if heading else text)
    if not parts:
        return None
    return "\n\n".join(parts)[:MAX_DESCRIPTION_LEN]


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = _LI_OPEN_RE.sub("\n- ", value)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
    return text or None
