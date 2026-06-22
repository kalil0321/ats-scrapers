"""Workable scraper.

Public widget API:
    https://apply.workable.com/api/v1/widget/accounts/{slug}

Returns a single JSON payload with ``jobs[]``. No auth. The widget
response carries title/department/location/employment_type/dates but
not the description body.

For descriptions we use Workable's per-job Markdown endpoint:

    GET https://apply.workable.com/{slug}/jobs/view/{shortcode}.md

That ships a clean Markdown render of the full posting (description +
requirements + benefits) with a header line carrying location /
employment-type / posted-date metadata.

Workable rate-limits hard from a single IP — bulk pipeline runs at
concurrency >2 see 429s on most tenants. The scraper retries 429/5xx
with exponential backoff (honouring ``Retry-After`` when present); the
caller should still keep concurrency low (2-4) for full re-scrapes.
The per-job Markdown fetch is best-effort so the listing row survives
when a detail request is rate-limited or unavailable.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_TEMPLATE = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
MARKDOWN_TEMPLATE = (
    "https://apply.workable.com/{slug}/jobs/view/{shortcode}.md"
)
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
USER_AGENT = "Mozilla/5.0 (compatible; jobhive/1.0)"
DETAIL_CONCURRENCY = 4  # rate-limit-safe pool size for per-job .md fetches

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "freelance": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
}

_HEADER_RE = re.compile(r"^#+\s+", re.MULTILINE)


@ScraperRegistry.register(ATSType.WORKABLE)
class WorkableScraper(BaseScraper):
    ats = ATSType.WORKABLE

    def fetch(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        response = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = httpx.get(
                    url,
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Workable fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                time.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Workable account not found: {self.company_slug}"
                )
            if response.status_code == 200:
                break
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Workable returned {response.status_code} for "
                        f"{self.company_slug} after {MAX_RETRIES} attempts"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.replace(".", "").isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                time.sleep(delay)
                continue
            raise ScraperError(
                f"Workable returned {response.status_code} for {self.company_slug}"
            )
        if response is None or response.status_code != 200:
            raise ScraperError(
                f"Workable exhausted retries for {self.company_slug}"
            )

        payload = response.json()
        jobs = [self._parse_job(item) for item in payload.get("jobs", [])]

        if self.include_descriptions and jobs:
            self._enrich_descriptions(jobs)
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        if not job.ats_id:
            return None
        url = MARKDOWN_TEMPLATE.format(slug=self.company_slug, shortcode=job.ats_id)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/markdown"},
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        text = response.text.strip()
        return text[:25_000] if text else None

    def _enrich_descriptions(self, jobs: list[Job]) -> None:
        """Pull the Markdown body for each job from
        ``/{slug}/jobs/view/{shortcode}.md``. Runs in a thread pool with
        a low cap (4) so we stay below the per-tenant rate limit."""

        def fetch_one(job: Job) -> None:
            description = self.get_description(job)
            if description and not job.description:
                job.description = description

        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            list(pool.map(fetch_one, jobs))

    def _parse_job(self, item: dict[str, Any]) -> Job:
        url = item.get("url") or item.get("application_url")
        apply_url = item.get("application_url")
        # Workable's "type" mirrors employment shape (full-time, contract, etc.)
        commitment_raw = item.get("type") or item.get("employment_type")
        commitment = (
            commitment_raw.strip()
            if isinstance(commitment_raw, str) and commitment_raw.strip()
            else None
        )

        # Map the freeform string to the canonical employment-type enum.
        employment_type: str | None = None
        if commitment:
            norm = commitment.lower()
            for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                if needle in norm:
                    employment_type = mapped
                    break

        is_remote = None
        if isinstance(item.get("telecommuting"), bool):
            is_remote = item["telecommuting"]
        elif isinstance(item.get("remote"), bool):
            is_remote = item["remote"]

        raw: dict[str, Any] = {}
        for k in ("department", "function", "industry", "experience",
                  "education", "language", "locations"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=item["title"],
            company=self.company_slug,
            ats_type=ATSType.WORKABLE,
            ats_id=item.get("shortcode") or item.get("code") or str(item.get("id", "")),
            location=_extract_location(item),
            is_remote=is_remote,
            department=item.get("department") if isinstance(item.get("department"), str) else None,
            employment_type=employment_type,
            commitment=commitment,
            apply_url=apply_url if isinstance(apply_url, str) and apply_url != url else None,
            posted_at=_parse_iso(item.get("published_on") or item.get("created_at")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _extract_location(item: dict[str, Any]) -> str | None:
    """Workable exposes location two ways:
    - flat fields `city`, `state`, `country` at the top level
    - structured `locations` array of dicts (more recent API)
    `location: {city, region, country}` shows up in the widget payload too.
    Try the richest representation first.
    """
    locs = item.get("locations") or []
    if isinstance(locs, list) and locs:
        first = locs[0] if isinstance(locs[0], dict) else {}
        parts = [first.get("city"), first.get("region"), first.get("country")]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined
    nested = item.get("location") or {}
    if isinstance(nested, dict) and nested:
        parts = [nested.get("city"), nested.get("region"), nested.get("country")]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined
    parts = [item.get("city"), item.get("state"), item.get("country")]
    return ", ".join(p for p in parts if p) or None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
