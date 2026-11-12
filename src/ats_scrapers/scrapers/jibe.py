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
* Paging stops at page 100. Later pages repeat page 100, so one query
  can return at most 10,000 postings.

Tenants above the cap are split by state. The state list comes from
the ``state`` facet when the site exposes it (``filter.facetList.state``)
and otherwise from the location list (``filter.locations.all``); the
``state`` query parameter filters on it either way. A state that is
itself above the cap is split again by category. Postings without a
state are then picked up category by category. The ``descending``
parameter does not change the order, so walking a list "from the other
end" is not an option.

The scraper fails closed: a walk that ends with clearly fewer postings
than the API announced raises instead of returning a partial board.

``company_slug`` is the career-site host or URL.
"""

from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote, urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 100
MAX_PAGE = 100  # pages past this one repeat it
QUERY_CAP = PAGE_SIZE * MAX_PAGE
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
            self._absorb(first, jobs)

            if total <= QUERY_CAP:
                # One list holds everything. Rows repeat a requisition per
                # language or location, so count rows, not unique postings.
                rows_seen = len(first["jobs"])
                rows_seen += await self._walk(fetch, {}, total, jobs, start=2)
                if rows_seen < math.floor(total * MIN_COVERAGE):
                    raise ScraperError(
                        f"Jibe ({self.host}) returned {rows_seen} of {total} "
                        "announced rows"
                    )
                return list(jobs.values())
            else:
                expected = _expected_unique(first, total)
                categories = _categories(first)
                for term, count in _state_facet(first) or _states_from_locations(first):
                    if count <= QUERY_CAP:
                        await self._walk(fetch, {"state": term}, count, jobs)
                        continue
                    for category, _ in categories:
                        await self._walk(
                            fetch, {"state": term, "categories": category}, QUERY_CAP, jobs,
                        )
                if len(jobs) < expected:
                    # Postings without a state.
                    for category, count in categories:
                        await self._walk(fetch, {"categories": category}, count, jobs)
                if len(jobs) < expected:
                    await self._walk(fetch, {}, total, jobs, start=2)

        expected = _expected_unique(first, total)
        if len(jobs) < math.floor(expected * MIN_COVERAGE):
            raise ScraperError(
                f"Jibe ({self.host}) returned {len(jobs)} of {expected} "
                "announced postings"
            )
        return list(jobs.values())

    async def _walk(
        self,
        fetch: Fetcher,
        filters: dict[str, str],
        expected: int,
        jobs: dict[str, Job],
        *,
        start: int = 1,
    ) -> int:
        """Walk one filtered list; return how many rows its pages held."""
        rows_seen = 0
        last_page = min(MAX_PAGE, max(1, math.ceil(expected / PAGE_SIZE)))
        for page in range(start, last_page + 1):
            payload = await self._page(fetch, page, filters)
            rows = self._absorb(payload, jobs)
            rows_seen += rows
            if rows < PAGE_SIZE:
                break
        return rows_seen

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
        payload = await fetch.get_json(f"{self.base_url}/api/jobs", params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ScraperError(
                f"Jibe ({self.host}) returned an unexpected payload on page {page}"
            )
        return payload

    def _absorb(self, payload: dict[str, Any], jobs: dict[str, Job]) -> int:
        """Add a page's postings to ``jobs``; return how many rows the page held."""
        rows = payload["jobs"]
        for row in rows:
            data = row.get("data") if isinstance(row, dict) else None
            if not isinstance(data, dict):
                raise ScraperError(f"Jibe ({self.host}) returned a posting without data")
            job = self._parse_job(data)
            if job is not None and job.ats_id:
                # English first: the list is not ordered by language, so a
                # translated twin never replaces a row already kept.
                existing = jobs.get(job.ats_id)
                if existing is None or (
                    (existing.language or "") != "en" and job.language == "en"
                ):
                    jobs[job.ats_id] = job
        return len(rows)

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


def _expected_unique(payload: dict[str, Any], total: int) -> int:
    """Postings we should hold once translated twins are merged.

    ``totalCount`` counts every language; ``languageCounts`` splits it.
    The largest single-language count is the floor for unique postings.
    """
    counts = payload.get("languageCounts")
    if isinstance(counts, dict):
        per_language = [
            entry["count"]
            for entry in counts.values()
            if isinstance(entry, dict)
            and isinstance(entry.get("count"), int)
            and not isinstance(entry.get("count"), bool)
        ]
        if per_language:
            return min(total, max(per_language))
    return total


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
