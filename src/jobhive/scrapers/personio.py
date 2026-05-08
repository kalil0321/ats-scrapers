"""Personio scraper.

Each Personio tenant is hosted at `{slug}.jobs.personio.com` (or `.com`/`.de`).
Two listing endpoints work in practice:

    GET https://{slug}.jobs.personio.com/search.json
    GET https://{slug}.jobs.personio.com/api/careers/jobs/list/

The listing returns title/department/office/schedule plus an
``employment_type`` label, but the ``description`` field is always
empty on the public board. Full description body lives on each job's
HTML detail page inside ``<div class="page_jobDescription...">`` —
we fan out per-job HTML fetches with a small concurrent pool to fill
descriptions.

The `slug` argument can be either the bare slug or the full base URL.
"""

from __future__ import annotations

import html as html_mod
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

ENDPOINTS = ("/search.json", "/api/careers/jobs/list/")
DETAIL_CONCURRENCY = 8

_TAG_RE = re.compile(r"<[^>]+>")
_DESC_CLASS_RE = re.compile(r"page_jobDescription", re.IGNORECASE)

# Personio's ``employment_type`` is freeform — typical values include
# "Permanent employee", "Working student", "Internship", "Trainee",
# "Freelancer", "Fixed term contract".
_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "trainee": "INTERN",
    "working student": "INTERN",
    "apprentice": "INTERN",
    "freelance": "CONTRACT",
    "freelancer": "CONTRACT",
    "contract": "CONTRACT",
    "fixed term": "CONTRACT",
    "fixed-term": "CONTRACT",
    "temp": "TEMPORARY",
    "temporary": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
}


@ScraperRegistry.register(ATSType.PERSONIO)
class PersonioScraper(BaseScraper):
    ats = ATSType.PERSONIO

    def fetch(self) -> list[Job]:
        base = self._resolve_base_url()
        last_error: Exception | None = None
        jobs: list[Job] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for path in ENDPOINTS:
                try:
                    response = client.get(f"{base}{path}")
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue
                if response.status_code == 404:
                    continue
                if response.status_code != 200:
                    last_error = ScraperError(f"Personio returned {response.status_code}")
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                items = _normalize_items(payload)
                if items:
                    jobs = [self._parse_job(item, base) for item in items]
                    break
            if jobs:
                self._enrich_descriptions(jobs)
                return jobs
        if last_error:
            raise CompanyNotFoundError(
                f"Personio tenant {self.company_slug} did not respond on any known endpoint"
            ) from last_error
        return []

    def _enrich_descriptions(self, jobs: list[Job]) -> None:
        """Fan out per-job HTML fetches; pull the description body out
        of the ``page_jobDescription`` block. ``httpx.Client`` is not
        thread-safe so we use short-lived per-thread clients."""
        timeout = self.timeout

        def fetch_one(job: Job) -> None:
            try:
                with httpx.Client(
                    timeout=timeout, follow_redirects=True,
                ) as client:
                    response = client.get(
                        str(job.url),
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
            except httpx.HTTPError:
                return
            if response.status_code != 200:
                return
            description = _extract_description(response.text)
            if description:
                job.description = description[:10_000]

        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            list(pool.map(fetch_one, jobs))

    def _resolve_base_url(self) -> str:
        slug = self.company_slug
        if slug.startswith(("http://", "https://")):
            return slug.rstrip("/")
        return f"https://{slug}.jobs.personio.com"

    def _parse_job(self, item: dict[str, Any], base: str) -> Job:
        ats_id = str(item.get("id") or item.get("jobId") or item.get("uuid") or "")
        commitment = item.get("schedule") or item.get("employmentType")

        # Map Personio's freeform ``employment_type`` (and the
        # ``schedule`` fallback) to the canonical enum.
        employment_type: str | None = None
        for label in (
            item.get("employment_type"),
            item.get("employmentType"),
            item.get("schedule"),
        ):
            if isinstance(label, str) and label.strip():
                norm = label.strip().lower()
                for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                    if needle in norm:
                        employment_type = mapped
                        break
                if employment_type:
                    break

        raw: dict[str, Any] = {}
        for k in ("subcompany", "department", "office", "occupation",
                  "occupationCategory", "yearsOfExperience",
                  "employment_type", "schedule", "category"):
            v = item.get(k)
            if v:
                raw[k] = v

        department = item.get("department")
        if isinstance(department, dict):
            department = department.get("name")

        return Job(
            url=item.get("url") or f"{base}/job/{ats_id}",
            title=item.get("name") or item.get("title") or item.get("subcompany"),
            company=urlparse(base).hostname or self.company_slug,
            ats_type=ATSType.PERSONIO,
            ats_id=ats_id,
            location=_extract_location(item),
            department=department if isinstance(department, str) else None,
            employment_type=employment_type,
            commitment=commitment if isinstance(commitment, str) else None,
            posted_at=_parse_iso(item.get("createdAt") or item.get("created_at")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _extract_description(html: str) -> str | None:
    """Pull the description body from a Personio detail page.

    Personio renders the body inside a ``<div class="page_jobDescription...">``
    block (the suffix is a build-hashed CSS-modules class so we match
    on the prefix). Returns plain text with whitespace collapsed.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None
    soup = BeautifulSoup(html, "html.parser")
    block = soup.find(class_=_DESC_CLASS_RE)
    if block is None:
        return None
    text = block.get_text(separator="\n", strip=True)
    text = html_mod.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _normalize_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("data", "jobs", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [p for p in value if isinstance(p, dict)]
    return []


def _extract_location(item: dict[str, Any]) -> str | None:
    if isinstance(item.get("office"), str):
        return item["office"]
    loc = item.get("location") or item.get("office") or {}
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("city")
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
