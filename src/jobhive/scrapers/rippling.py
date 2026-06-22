"""Rippling ATS scraper.

Two-step API:

* Listing: ``GET /platform/api/ats/v1/board/{slug}/jobs`` — returns
  every active posting with id/title/department/workLocation but
  no description or dates.
* Detail: ``GET /platform/api/ats/v1/board/{slug}/jobs/{id}`` — adds
  ``description`` (split into ``company`` + ``role`` HTML),
  ``employmentType`` (dict with ``label`` enum + ``id`` display
  string), ``createdOn`` ISO timestamp, ``workLocations`` array, and
  ``payRangeDetails``.

We fan out detail fetches concurrently (capped at
``DETAIL_CONCURRENCY``) so a tenant with 50 open positions still
finishes in a few seconds.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_TEMPLATE = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
DETAIL_TEMPLATE = (
    "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{id}"
)
DETAIL_CONCURRENCY = 8

_TAG_RE = re.compile(r"<[^>]+>")

# Rippling's ``employmentType.label`` is a stable enum.
_EMPLOYMENT_TYPE_MAP = {
    "SALARIED_FT": "FULL_TIME",
    "SALARIED_PT": "PART_TIME",
    "HOURLY_FT": "FULL_TIME",
    "HOURLY_PT": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "CONTRACT": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
}


@ScraperRegistry.register(ATSType.RIPPLING)
class RipplingScraper(BaseScraper):
    ats = ATSType.RIPPLING

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_detail(client, sem, copy)
            return copy.description

        return asyncio.run(run())

    async def _fetch_async(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            try:
                response = await client.get(
                    url, headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise ScraperError(
                    f"Rippling fetch failed for {self.company_slug}: {exc}"
                ) from exc
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Rippling board not found: {self.company_slug}"
                )
            if response.status_code != 200:
                raise ScraperError(
                    f"Rippling returned {response.status_code} for "
                    f"{self.company_slug}"
                )

            payload = response.json()
            if isinstance(payload, dict):
                items = payload.get("items") or payload.get("jobs") or []
            elif isinstance(payload, list):
                items = payload
            else:
                items = []
            jobs = [self._parse_job(item) for item in items]

            if self.include_descriptions and jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(client, sem, j) for j in jobs
                ))
        return jobs

    async def _enrich_detail(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        if not job.ats_id:
            return
        url = DETAIL_TEMPLATE.format(slug=self.company_slug, id=job.ats_id)
        async with sem:
            try:
                response = await client.get(
                    url, headers={"Accept": "application/json"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        try:
            data = response.json()
        except ValueError:
            return
        _apply_detail_to_job(job, data)

    def _parse_job(self, item: dict[str, Any]) -> Job:
        # ``department`` arrives as ``{id, label}``; the label is what
        # users see in the careers UI.
        dept = item.get("department")
        department = (
            dept.get("label") or dept.get("id") if isinstance(dept, dict) else dept
        )
        if not isinstance(department, str):
            department = None

        raw: dict[str, Any] = {}
        for k in ("department", "team", "employmentType", "workLocation",
                  "workType", "experienceLevel", "compensation"):
            v = item.get(k)
            if v:
                raw[k] = v

        ats_id = str(item.get("uuid") or item.get("id") or "")
        return Job(
            url=item.get("url")
            or item.get("hostedUrl")
            or f"https://ats.rippling.com/{self.company_slug}/jobs/{ats_id}",
            title=item.get("name") or item.get("title") or "Untitled",
            company=self.company_slug,
            ats_type=ATSType.RIPPLING,
            ats_id=ats_id,
            location=_extract_location(item),
            department=department,
            commitment=item.get("employmentType")
            if isinstance(item.get("employmentType"), str) else None,
            posted_at=_parse_iso(
                item.get("createdAt") or item.get("created_at")
            ),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _apply_detail_to_job(job: Job, detail: dict[str, Any]) -> None:
    """Hydrate ``job`` from the ``/jobs/{id}`` detail payload.

    Detail-only fields:

    * ``description`` (dict with ``company`` + ``role`` HTML) — strip
      tags and concatenate. The ``role`` section is the actual job
      body; ``company`` is the about-us blurb. Both are preserved.
    * ``employmentType`` (dict with ``label`` enum + ``id`` display
      string) — map the label to our canonical enum.
    * ``createdOn`` (ISO timestamp).
    * ``workLocations`` (array) — fall through when the listing
      didn't surface one.
    * ``payRangeDetails`` — usually an empty list, kept in raw.
    """
    desc_obj = detail.get("description")
    if isinstance(desc_obj, dict) and not job.description:
        parts: list[str] = []
        for key in ("role", "company"):
            html = desc_obj.get(key)
            if isinstance(html, str) and html.strip():
                parts.append(_strip_html(html))
        if parts:
            job.description = "\n\n".join(parts)[:25_000]

    emp = detail.get("employmentType")
    if isinstance(emp, dict):
        label = emp.get("label")
        if isinstance(label, str):
            mapped = _EMPLOYMENT_TYPE_MAP.get(label.strip().upper())
            if mapped and not job.employment_type:
                job.employment_type = mapped
        # ``id`` is the user-facing label ("Salaried, full-time").
        commitment_id = emp.get("id")
        if isinstance(commitment_id, str) and not job.commitment:
            job.commitment = commitment_id.strip() or None

    created = detail.get("createdOn")
    if isinstance(created, str) and not job.posted_at:
        job.posted_at = _parse_iso(created)

    if not job.location:
        locs = detail.get("workLocations")
        if isinstance(locs, list) and locs:
            first = locs[0]
            if isinstance(first, str):
                job.location = first.strip() or None
            elif isinstance(first, dict):
                label = first.get("label") or first.get("displayName")
                if isinstance(label, str):
                    job.location = label.strip() or None


def _extract_location(item: dict[str, Any]) -> str | None:
    loc = item.get("workLocation") or item.get("location") or {}
    if isinstance(loc, str):
        return loc.strip() or None
    if isinstance(loc, dict):
        for key in ("displayName", "label", "city", "country"):
            v = loc.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _strip_html(text: str) -> str:
    out = _TAG_RE.sub(" ", text)
    out = html_mod.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
