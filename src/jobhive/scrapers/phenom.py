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
remaining offsets concurrently.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

PAGE_SIZE = 100  # Phenom accepts up to 100 per page.
MAX_CONCURRENCY = 8
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_CSRF_RE = re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
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
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        if not company_slug.startswith(("http://", "https://")):
            raise ScraperError(
                f"Phenom slug must be a full URL (e.g. https://jobs.bell.ca), "
                f"got {company_slug!r}"
            )
        self.base_url = company_slug.rstrip("/")
        self.locale = locale
        self.country = country
        host = urlparse(self.base_url).hostname or company_slug
        self.company_name = host

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            csrf = await self._init_session(client)
            first = await self._search(client, csrf, start=0)
            jobs_first = self._extract_jobs(first)
            total = self._extract_total(first)

            seen: set[str] = set()
            all_jobs: list[Job] = []
            for item in jobs_first:
                job = self._parse_job(item)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                all_jobs.append(job)

            if not total or total <= len(jobs_first):
                return all_jobs

            offsets = list(range(len(jobs_first), total, PAGE_SIZE))
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def task(offset: int) -> None:
                async with sem:
                    payload = await self._search(client, csrf, start=offset)
                    for item in self._extract_jobs(payload):
                        job = self._parse_job(item)
                        if job is None or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        all_jobs.append(job)

            await asyncio.gather(*(task(o) for o in offsets))
            return all_jobs

    # --- session / csrf -------------------------------------------------

    async def _init_session(self, client: httpx.AsyncClient) -> str | None:
        """Seed cookies + extract CSRF token. The POST /widgets endpoint
        requires both — without them it returns 403."""
        search_url = self._search_results_url()
        try:
            response = await client.get(search_url, headers=_BASE_HEADERS)
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"Phenom session init failed for {self.base_url}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"Phenom careers site not found: {self.base_url}"
            )
        if response.status_code != 200:
            raise ScraperError(
                f"Phenom session init returned {response.status_code} for "
                f"{self.base_url}"
            )
        # Cookie-based csrf is the canonical path; some tenants only embed
        # the token in the page HTML.
        for cookie in client.cookies.jar:
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
        client: httpx.AsyncClient,
        csrf: str | None,
        *,
        start: int,
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
            "selected_fields": {},
            "locationData": {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": self.base_url,
            "Referer": self._search_results_url(),
            "User-Agent": "Mozilla/5.0",
        }
        if csrf:
            headers["x-csrf-token"] = csrf

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(
                    f"{self.base_url}/widgets",
                    json=payload,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Phenom POST /widgets failed for {self.base_url} at "
                        f"start={start}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Phenom returned malformed JSON for {self.base_url}: {exc}"
                    ) from exc
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Phenom ({self.base_url}) returned "
                        f"{response.status_code} at start={start} after "
                        f"{MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Phenom returned {response.status_code} at start={start} "
                f"for {self.base_url}"
            )
        raise ScraperError(
            f"Phenom ({self.base_url}) exhausted retries at start={start}"
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
            url = f"{self.base_url}/job/{ats_id}"
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
            commitment=commitment,
            requisition_id=str(item.get("jobSeqNo")) if item.get("jobSeqNo") else None,
            description=_clean_description(item.get("descriptionTeaser") or item.get("description")),
            posted_at=_parse_iso(item.get("postedDate") or item.get("dateCreated") or item.get("createdAt")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


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
    cleaned = re.sub(r"<[^>]+>", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:10_000]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
