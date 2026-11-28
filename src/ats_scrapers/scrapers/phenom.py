"""Phenom (PhenomPeople) scraper.

Phenom-powered career sites (Bell Canada, GE Healthcare, T-Mobile, etc.) all
share the same search widget endpoint:

    POST {base_url}/widgets

with a JSON payload that ``deviceType``, ``country``, ``lang``, pagination,
and the magic ``ddoKey: "refineSearch"`` and ``pageName: "search-results"``.
The endpoint requires a CSRF token that's seeded by a prior GET to:

    GET {base_url}/{country}/{lang}/search-results

The CSRF token comes back as a cookie; we replay it on the POST.

Tenants vary on ``country`` (``"ca"``, ``"global"``, ``"us"``) and ``locale``
(``"en_ca"``, ``"en_global"``, ``"en_us"``) — pass them at construction.

Pagination: the first response includes ``totalHits``; we fan out the
remaining offsets concurrently. The search serves only the first
``RESULT_WINDOW`` rows of a query, so a larger search is split by facet
(category, then state, ...) until every part fits.

Most sites list only ``descriptionTeaser``, the posting's first 200 to 300
characters. The full text comes from the same endpoint, one job at a time::

    POST {base_url}/widgets  {"ddoKey": "jobDetail", "jobId": ..., ...}

``afetch`` reads it for every such job when ``include_descriptions`` is set;
otherwise ``get_description`` reads it on demand.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._engagement import engagement_from_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 100  # Phenom accepts up to 100 per page.
MAX_CONCURRENCY = 8
# Pages past this many rows come back empty while ``totalHits`` still counts
# every job.
RESULT_WINDOW = 9_900
# A site that refuses the posting widget this many times in a row refuses it
# for every job: the rest keep their teasers without a request each.
MAX_POSTING_REFUSALS = 5
# Facets that split a search larger than the window, in order of preference.
# ``city`` is last: its aggregation lists at most 1,000 values.
PARTITION_FIELDS = ("category", "state", "country", "city")

logger = logging.getLogger(__name__)

_CSRF_RE = re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')
_SITE_PATH_RE = re.compile(r"^/([a-z]{2,6})/([a-z]{2})(?:/|$)")
_PAGE_BASE_URL_RE = re.compile(r'"baseUrl"\s*:\s*"(https?://[^"]+)"')

# Akamai in front of some sites holds a request whose agent is a bare
# "Mozilla/5.0" until it times out.
_USER_AGENT = "Mozilla/5.0 (compatible; ats-scrapers/1.0)"
# Every page of a Phenom site loads its client from these names.
_PHENOM_PAGE_MARKERS = ("phApp", "phenompeople")
# Imperva Incapsula in front of some sites answers the first requests of a
# session with a challenge page that loads this script, then lets the same
# session through.
_INCAPSULA_MARKER = "/_Incapsula_Resource"
CHALLENGE_ATTEMPTS = 3

_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Phenom's ``jobType`` is freeform per tenant (``"Full Time"``,
# ``"Part Time"``, ``"Regular"``, ``"Other"``, ``"Intern"``…). Map the
# common ones to the canonical employment-type enum.
_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "co-op": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "freelance": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "temporary": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "casual": "TEMPORARY",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "regular": "FULL_TIME",
    "permanent": "FULL_TIME",
}


@ScraperRegistry.register(ATSType.PHENOM)
class PhenomScraper(BaseScraper):
    """Phenom scraper. ``company_slug`` is the full base URL of the careers
    site (e.g. ``"https://jobs.bell.ca"``). ``locale`` and ``country`` are
    tenant-specific and typically appear in the public URL path.

    The canonical tenant list at ``ats-companies/phenom.csv`` ships the
    correct ``locale``/``country`` per tenant (columns:
    ``url,name,company_code,locale,country``). For brand-new tenants the
    default ``"en_us"``/``"us"`` works for the majority of US sites."""

    ats = ATSType.PHENOM

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        locale: str = "en_us",
        country: str = "us",
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if not company_slug.startswith(("http://", "https://")):
            raise ScraperError(
                f"Phenom slug must be a full URL (e.g. https://jobs.bell.ca), "
                f"got {company_slug!r}"
            )
        self.base_url = company_slug.rstrip("/")
        self._resolved_site_path: str | None = None
        self.locale = locale
        self.country = country
        host = urlparse(self.base_url).hostname or company_slug
        self.company_name = host
        # Listing teasers of the jobs listed without their full text, by job
        # id: what a job keeps when its posting cannot be read.
        self._teasers: dict[str, str] = {}
        self._refusals = 0

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher(missing_host_not_found=True) as fetch:
            csrf = await self._init_session(fetch)
            jobs: dict[str, Job] = {}
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            total = await self._collect(fetch, csrf, {}, sem, jobs)
            if total and len(jobs) < total:
                logger.warning(
                    "Phenom (%s) announced %d jobs, collected %d",
                    self.base_url, total, len(jobs),
                )
            if self.include_descriptions:
                await asyncio.gather(*(
                    self._fill_description(fetch, csrf, sem, job)
                    for job in jobs.values()
                    if not job.description
                ))
            return list(jobs.values())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                return await self._description(fetch, None, job)

        return self._run_sync(run())

    async def _fill_description(
        self,
        fetch: Fetcher,
        csrf: str | None,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            job.description = await self._description(fetch, csrf, job)

    async def _description(
        self,
        fetch: Fetcher,
        csrf: str | None,
        job: Job,
    ) -> str | None:
        """The posting's full text, or its listing teaser when it cannot be read."""
        if self._refusals < MAX_POSTING_REFUSALS:
            try:
                description = await self._posting_description(fetch, csrf, job.ats_id)
            except (ScraperError, ValueError) as exc:
                self._refusals += 1
                logger.warning("Phenom posting %s could not be read: %s", job.url, exc)
            else:
                self._refusals = 0
                if description:
                    return description
        return self._teasers.get(job.ats_id)

    async def _posting_description(
        self,
        fetch: Fetcher,
        csrf: str | None,
        job_id: str,
    ) -> str | None:
        """The full text of one posting, from the job page's own widget."""
        payload = {
            "lang": self.locale,
            "deviceType": "desktop",
            "country": self.country,
            "pageName": "job",
            "ddoKey": "jobDetail",
            "jobId": job_id,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": self.base_url,
            "Referer": self._job_url(job_id),
            "User-Agent": _USER_AGENT,
        }
        if csrf:
            headers["x-csrf-token"] = csrf
        response = await fetch.request(
            "POST", f"{self.base_url}/widgets", json=payload, headers=headers
        )
        body = response.json()
        detail = body.get("jobDetail") if isinstance(body, dict) else None
        data = detail.get("data") if isinstance(detail, dict) else None
        posting = data.get("job") if isinstance(data, dict) else None
        if not isinstance(posting, dict) or str(posting.get("jobId") or "") != job_id:
            return None
        return _clean_description(posting.get("description"))

    async def _collect(
        self,
        fetch: Fetcher,
        csrf: str | None,
        selected: dict[str, list[str]],
        sem: asyncio.Semaphore,
        jobs: dict[str, Job],
    ) -> int | None:
        """Read every job matching ``selected`` into ``jobs``; return the
        announced count."""
        async with sem:
            first = await self._search(fetch, csrf, start=0, selected=selected)
        self._add_jobs(first, jobs)
        total = self._extract_total(first)
        first_page = len(self._extract_jobs(first))
        if not total or total <= first_page:
            return total

        if total > RESULT_WINDOW:
            split = _partition(first, selected, total)
            if split is not None:
                field, values = split
                await asyncio.gather(*(
                    self._collect(fetch, csrf, {**selected, field: [value]}, sem, jobs)
                    for value in values
                ))
                return total

        async def page(offset: int) -> None:
            async with sem:
                payload = await self._search(
                    fetch, csrf, start=offset, selected=selected
                )
            self._add_jobs(payload, jobs)

        await asyncio.gather(*(
            page(offset)
            for offset in range(first_page, min(total, RESULT_WINDOW), PAGE_SIZE)
        ))
        return total

    def _add_jobs(self, payload: dict[str, Any], jobs: dict[str, Job]) -> None:
        for item in self._extract_jobs(payload):
            job = self._parse_job(item)
            if job is not None and job.ats_id not in jobs:
                jobs[job.ats_id] = job

    # --- session / csrf -------------------------------------------------

    async def _init_session(self, fetch: Fetcher) -> str | None:
        """Seed cookies + extract CSRF token. The POST /widgets endpoint
        requires both — without them it returns 403.

        The Fetcher's persistent httpx client keeps the seeded cookies and
        replays them on the subsequent POSTs automatically; we only reach
        into its cookie jar to read the CSRF token's *value* for the
        ``x-csrf-token`` header."""
        search_url = self._search_results_url()
        response = await fetch.request("GET", search_url, headers=_BASE_HEADERS)
        if _left_phenom(search_url, response.url, response.text):
            raise CompanyNotFoundError(
                f"Phenom site {self.base_url} redirects to {response.url}"
            )
        self._resolved_site_path = _site_path_of(response.text, self.base_url)
        # Cookie-based csrf is the canonical path; some tenants only embed
        # the token in the page HTML.
        for cookie in fetch.cookies.jar:
            if "csrf" in cookie.name.lower():
                return cookie.value
        match = _CSRF_RE.search(response.text)
        return match.group(1) if match else None

    def _search_results_url(self) -> str:
        # `lang.split("_")[0]` → "en" from "en_ca" — Phenom URLs use the
        # bare language, not the locale.
        lang = self.locale.split("_", 1)[0]
        return f"{self.base_url}/{self.country}/{lang}/search-results"

    # --- search request -------------------------------------------------

    async def _search(
        self,
        fetch: Fetcher,
        csrf: str | None,
        *,
        start: int,
        selected: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "lang": self.locale,
            "deviceType": "desktop",
            "country": self.country,
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "",
            "subsearch": "",
            "from": start,
            "jobs": True,
            "counts": True,
            "all_fields": [
                "category", "jobFamilies", "country", "state", "city",
                "experienceLevel",
            ],
            "size": PAGE_SIZE,
            "clearAll": False,
            "jdsource": "facets",
            "isSliderEnable": False,
            "pageId": "page20",
            "siteType": "external",
            "keywords": "",
            "global": True,
            "selected_fields": selected or {},
            "locationData": {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": self.base_url,
            "Referer": self._search_results_url(),
            "User-Agent": _USER_AGENT,
        }
        if csrf:
            headers["x-csrf-token"] = csrf

        for _ in range(CHALLENGE_ATTEMPTS):
            response = await fetch.request(
                "POST",
                f"{self.base_url}/widgets",
                json=payload,
                headers=headers,
            )
            try:
                return response.json()
            except ValueError as exc:
                if _INCAPSULA_MARKER not in response.text:
                    raise ScraperError(
                        f"Phenom returned malformed JSON for {self.base_url}: {exc}"
                    ) from exc
        raise ScraperError(
            f"Phenom ({self.base_url}) answered the search with an Imperva "
            f"Incapsula bot challenge {CHALLENGE_ATTEMPTS} times"
        )

    # --- extraction ----------------------------------------------------

    def _extract_jobs(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Phenom's response shape varies — check the known paths in order
        of likelihood."""
        rs = payload.get("refineSearch") or {}
        if isinstance(rs, dict):
            data = rs.get("data") or {}
            if isinstance(data.get("jobs"), list):
                return data["jobs"]
            if isinstance(rs.get("jobs"), list):
                return rs["jobs"]
            if isinstance(rs.get("hits"), list):
                return rs["hits"]
        if isinstance(payload.get("jobs"), list):
            return payload["jobs"]
        return []

    def _extract_total(self, payload: dict[str, Any]) -> int | None:
        rs = payload.get("refineSearch") or {}
        if not isinstance(rs, dict):
            return None
        for path in (
            ("totalHits",),
            ("data", "totalHits"),
            ("hitsCount",),
        ):
            value: Any = rs
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
                if value is None:
                    break
            if isinstance(value, int):
                return value
        return None

    def _job_url(self, ats_id: str) -> str:
        """Job page link, under the site's ``/{country}/{lang}`` path."""
        return f"{self.base_url}{self._site_path()}/job/{ats_id}"

    def _site_path(self) -> str:
        if self._resolved_site_path:
            return self._resolved_site_path
        return f"/{self.country}/{self.locale.split('_', 1)[0]}"

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("jobId") or item.get("id") or "")
        if not ats_id:
            return None
        title = (
            item.get("title")
            or item.get("jobTitle")
            or "Untitled"
        )
        url = item.get("jobUrl") or item.get("url")
        if not url or not isinstance(url, str):
            url = self._job_url(ats_id)
        elif not url.startswith(("http://", "https://")):
            url = f"{self.base_url}{url if url.startswith('/') else '/' + url}"

        raw: dict[str, Any] = {}
        for k in ("category", "subCategory", "businessUnit",
                  "jobType", "jobFamily", "remoteType",
                  "jobSeqNo", "internalCategoryName"):
            v = item.get(k)
            if v:
                raw[k] = v

        # ``remoteType`` is the structured signal when present;
        # ``jobType`` sometimes carries "remote"/"hybrid" prefixes.
        is_remote: bool | None = None
        for source in (item.get("remoteType"), item.get("jobType"), item.get("locationType")):
            if isinstance(source, str):
                norm = source.strip().lower()
                if not norm:
                    continue
                if "remote" in norm or "wfh" in norm or "work from home" in norm:
                    is_remote = True
                    break
                if norm in ("onsite", "on-site", "in-office", "in office", "office"):
                    is_remote = False

        # Map ``jobType`` to the canonical ``employment_type`` enum;
        # keep the original label in ``commitment`` for display.
        commitment = (
            item.get("jobType") if isinstance(item.get("jobType"), str) else None
        )
        employment_type: str | None = None
        if commitment:
            norm = commitment.strip().lower()
            for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                if needle in norm:
                    employment_type = mapped
                    break

        description = _clean_description(item.get("description"))
        if description is None:
            teaser = _clean_description(item.get("descriptionTeaser"))
            if teaser:
                self._teasers[ats_id] = teaser

        return Job(
            url=url,
            title=title,
            company=self.company_name,
            ats_type=ATSType.PHENOM,
            ats_id=ats_id,
            location=_format_location(item),
            is_remote=is_remote,
            department=item.get("department") or item.get("category"),
            employment_type=employment_type,
            engagement_type=engagement_from_label(commitment),
            commitment=commitment,
            requisition_id=str(item.get("jobSeqNo")) if item.get("jobSeqNo") else None,
            description=description,
            posted_at=_parse_iso(item.get("postedDate") or item.get("dateCreated") or item.get("createdAt")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _partition(
    payload: dict[str, Any],
    selected: dict[str, list[str]],
    total: int,
) -> tuple[str, list[str]] | None:
    """The first facet whose values split ``payload``'s search, if any.

    A facet qualifies only when its counts add up to the whole search, so no
    job falls outside every part. Values combine with AND across facets.
    """
    rs = payload.get("refineSearch")
    data = rs.get("data") if isinstance(rs, dict) else None
    aggregations = data.get("aggregations") if isinstance(data, dict) else None
    counts: dict[str, dict[str, Any]] = {}
    for aggregation in aggregations if isinstance(aggregations, list) else []:
        if isinstance(aggregation, dict) and isinstance(aggregation.get("value"), dict):
            counts[str(aggregation.get("field"))] = aggregation["value"]
    for field in PARTITION_FIELDS:
        values = counts.get(field) or {}
        if field in selected or len(values) < 2:
            continue
        covered = sum(n for n in values.values() if isinstance(n, int))
        if covered >= total:
            return field, list(values)
    return None


def _format_location(item: dict[str, Any]) -> str | None:
    """Compose 'City, State, Country' from Phenom's split fields."""
    parts: list[str] = []
    for key in ("city", "state", "country"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if parts:
        return ", ".join(parts)
    direct = item.get("location") or item.get("cityState") or item.get("cityStateCountry")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return None


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:25_000]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _left_phenom(requested: str, answered: str | None, page: str) -> bool:
    """Whether the search page redirected to a page that is not Phenom's.

    A live site may redirect to another of its own pages, and a bot wall
    or a country block answers without redirecting.
    """
    if not answered:
        return False
    before, after = urlparse(requested), urlparse(answered)
    redirected = (before.hostname, before.path) != (after.hostname, after.path)
    return redirected and not any(
        marker in page for marker in _PHENOM_PAGE_MARKERS
    )


def _site_path_of(page: str, base_url: str) -> str | None:
    """``/{country}/{lang}`` the site says it lives under, if the page tells."""
    host = urlparse(base_url).hostname
    for candidate in _PAGE_BASE_URL_RE.findall(page):
        parsed = urlparse(candidate)
        match = _SITE_PATH_RE.match(parsed.path)
        if parsed.hostname == host and match:
            return f"/{match.group(1)}/{match.group(2)}"
    return None
