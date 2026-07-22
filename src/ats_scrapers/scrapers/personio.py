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

import asyncio
import html as html_mod
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

ENDPOINTS = ("/search.json", "/api/careers/jobs/list/")
DETAIL_CONCURRENCY = 8

# Endpoint fallback (listing) and per-job detail fetches are both
# scraper-driven: a 404 means "try the next endpoint" / "skip this
# job", never CompanyNotFoundError, so it comes back unmapped for the
# scraper to branch on. Everything else goes through the Fetcher's
# normal mapping — transient 429/5xx get retried before this scraper
# falls back to the next endpoint (or skips the description).
_HANDLED_STATUSES = frozenset({404})

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
        slug = company_slug.strip()
        if slug.startswith(("http://", "https://")):
            self.company_slug = require_http_url(slug, provider="PersonioScraper")
        else:
            self.company_slug = require_host_label(slug, provider="PersonioScraper")

    async def afetch(self) -> list[Job]:
        base = self._resolve_base_url()
        last_error: Exception | None = None
        jobs: list[Job] = []
        async with self.make_fetcher() as fetch:
            for path in ENDPOINTS:
                try:
                    response = await fetch.request(
                        "GET", f"{base}{path}", handled=_HANDLED_STATUSES,
                    )
                except ScraperError as exc:
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
                if self.include_descriptions:
                    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                    await asyncio.gather(*(
                        self._enrich_description(fetch, sem, j) for j in jobs
                    ))
                return jobs
        if last_error:
            raise CompanyNotFoundError(
                f"Personio tenant {self.company_slug} did not respond on any known endpoint"
            ) from last_error
        return []

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                return await self._fetch_description(fetch, job)

        return self._run_sync(run())

    async def _fetch_description(self, fetch: Fetcher, job: Job) -> str | None:
        """Pull the description body out of the detail page's
        ``page_jobDescription`` block. Best-effort — any error keeps
        the listing-derived row."""
        try:
            response = await fetch.request(
                "GET",
                str(job.url),
                headers={"User-Agent": "Mozilla/5.0"},
                handled=_HANDLED_STATUSES,
            )
        except ScraperError:
            return None
        if response.status_code != 200:
            return None
        description = _extract_description(response.text)
        return description[:25_000] if description else None

    async def _enrich_description(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        if job.description:
            return
        async with sem:
            description = await self._fetch_description(fetch, job)
        if description:
            job.description = description

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
            fetched_at=datetime.now(UTC),
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
