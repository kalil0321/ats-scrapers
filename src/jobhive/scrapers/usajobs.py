"""USAJOBS.gov public API scraper.

USAJOBS is the official US federal-government job board. It exposes a
documented JSON API at:

    GET https://data.usajobs.gov/api/Search

Authentication is by free API key (request at developer.usajobs.gov), sent
as the ``Authorization-Key`` header alongside ``Host`` and ``User-Agent``.
The key is read from the ``USAJOBS_API_KEY`` env var; the scraper raises a
clear :class:`ScraperError` if it's missing.

Pagination is server-side via ``Page`` and ``ResultsPerPage`` (max 500).
A typical run sees ~10-20K active postings across 200+ federal agencies.

Unlike the multi-tenant ATSes, USAJOBS is a *single source* — there's no
per-tenant slug. We accept any ``company_slug`` argument (used for logging
only) and pull the full active dataset on every fetch.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://data.usajobs.gov/api/Search"
PAGE_SIZE = 500  # API hard-caps at 500 per page.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
ENV_API_KEY = "USAJOBS_API_KEY"
ENV_USER_AGENT = "USAJOBS_USER_AGENT"  # Optional override; defaults to email below.
DEFAULT_USER_AGENT = "stapply-ai (open-source jobs dataset)"
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 25_000

_TYPE_MAP = {
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "intermittent": "TEMPORARY",
    "internships": "INTERN",
    "temporary": "TEMPORARY",
}


# Note: USAJOBS isn't an ATS in the multi-tenant sense — but it's a major
# employer-direct source, so it gets its own ``ATSType`` value.
@ScraperRegistry.register(ATSType.USAJOBS)
class USAJobsScraper(BaseScraper):
    """USAJOBS.gov scraper. Single-source: ``company_slug`` is unused.

    Reads ``USAJOBS_API_KEY`` from the environment. Optional
    ``USAJOBS_USER_AGENT`` overrides the User-Agent (the API expects an
    email address per their TOS).
    """

    ats = ATSType.USAJOBS

    def fetch(self) -> list[Job]:
        api_key = os.environ.get(ENV_API_KEY, "").strip()
        if not api_key:
            raise ScraperError(
                f"{ENV_API_KEY} env var is required. "
                f"Register at https://developer.usajobs.gov to get a free key."
            )
        user_agent = os.environ.get(ENV_USER_AGENT, DEFAULT_USER_AGENT)
        return asyncio.run(self._fetch_async(api_key, user_agent))

    async def _fetch_async(self, api_key: str, user_agent: str) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            page = 1
            while True:
                payload = await self._fetch_page(client, api_key, user_agent, page)
                result = payload.get("SearchResult") if isinstance(payload, dict) else {}
                if not isinstance(result, dict):
                    break
                items = result.get("SearchResultItems") or []
                if not items:
                    break
                for item in items:
                    job = self._parse_item(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                # Termination: stop when we've drained the reported page count.
                page_total = (
                    int(result.get("UserArea", {}).get("NumberOfPages") or 0)
                    if isinstance(result.get("UserArea"), dict) else 0
                )
                if page_total and page >= page_total:
                    break
                if len(items) < PAGE_SIZE:
                    break
                page += 1
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        user_agent: str,
        page: int,
    ) -> dict[str, Any]:
        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
            "Accept": "application/json",
        }
        params = {"ResultsPerPage": PAGE_SIZE, "Page": page}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(API_URL, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"USAJOBS fetch failed at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"USAJOBS returned malformed JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 401:
                raise ScraperError(
                    f"USAJOBS rejected the API key (401). Check {ENV_API_KEY}."
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"USAJOBS returned {response.status_code} at page={page} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"USAJOBS returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"USAJOBS exhausted retries at page={page}")

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        descriptor = (
            item.get("MatchedObjectDescriptor")
            if isinstance(item, dict) else None
        )
        if not isinstance(descriptor, dict):
            return None
        ats_id = str(descriptor.get("PositionID") or "").strip()
        title = (descriptor.get("PositionTitle") or "").strip()
        url = descriptor.get("PositionURI") or descriptor.get("ApplyURI") or None
        if isinstance(url, list) and url:
            url = url[0]
        if not ats_id or not title or not url:
            return None

        org = descriptor.get("OrganizationName") or descriptor.get("DepartmentName") or "USAJOBS"

        # Locations is a list of {"LocationName", "CountryCode", ...}
        locs = descriptor.get("PositionLocation") or []
        location = None
        if isinstance(locs, list) and locs:
            names = [
                str(loc.get("LocationName")).strip()
                for loc in locs
                if isinstance(loc, dict) and loc.get("LocationName")
            ]
            if names:
                location = "; ".join(names[:3])
                if len(names) > 3:
                    location += f" (+{len(names) - 3} more)"

        emp = (descriptor.get("PositionSchedule") or [{}])[0]
        emp_name = (
            emp.get("Name") if isinstance(emp, dict) else None
        )
        employment_type = _TYPE_MAP.get((emp_name or "").lower())

        # Salary: PositionRemuneration is a list of {MinimumRange, MaximumRange,
        # RateIntervalCode}. Take the first entry.
        rem = (descriptor.get("PositionRemuneration") or [{}])[0]
        if isinstance(rem, dict):
            salary_min = _to_float(rem.get("MinimumRange"))
            salary_max = _to_float(rem.get("MaximumRange"))
            salary_currency = "USD"
            interval = (rem.get("RateIntervalCode") or "").strip().lower()
            salary_period = {
                "per year": "YEAR",
                "per hour": "HOUR",
                "per month": "MONTH",
                "per week": "WEEK",
                "per day": "DAY",
            }.get(interval)
        else:
            salary_min = salary_max = None
            salary_currency = salary_period = None

        ud = descriptor.get("UserArea", {}).get("Details", {}) if isinstance(
            descriptor.get("UserArea"), dict
        ) else {}
        description_html = (
            ud.get("JobSummary") if isinstance(ud, dict) else None
        )

        apply_uri = descriptor.get("ApplyURI")
        if isinstance(apply_uri, list) and apply_uri:
            apply_uri = apply_uri[0]
        if not isinstance(apply_uri, str):
            apply_uri = None

        raw: dict[str, Any] = {}
        for k in ("DepartmentName", "JobCategory", "JobGrade",
                  "QualificationSummary", "PositionFormattedDescription",
                  "WhoMayApply", "SecurityClearanceRequired"):
            v = descriptor.get(k)
            if v:
                raw[k] = v

        return Job(
            url=str(url),
            title=title,
            company=str(org),
            ats_type=ATSType.USAJOBS,
            ats_id=ats_id,
            location=location,
            employment_type=employment_type,
            commitment=emp_name if isinstance(emp_name, str) else None,
            apply_url=apply_uri if apply_uri and apply_uri != url else None,
            requisition_id=ats_id if ats_id else None,
            description=_html_unescape_for_desc(description_html),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=salary_currency if salary_min or salary_max else None,
            salary_period=salary_period if salary_min or salary_max else None,
            posted_at=_parse_iso(descriptor.get("PublicationStartDate")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure.
    Replaces the legacy _strip_html/_html_to_text path for descriptions
    only — title/company/salary fields still use the strip variant."""
    import html as _h
    if not isinstance(value, str):
        return None
    out = _h.unescape(value).strip()
    if not out:
        return None
    return out[:cap]


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text[:MAX_DESCRIPTION_LEN] if text else None


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
