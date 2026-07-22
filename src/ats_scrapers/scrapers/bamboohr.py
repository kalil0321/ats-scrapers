"""BambooHR scraper.

BambooHR's old `/careers/list` JSON endpoint was deprecated in 2024 — every
tenant now serves a 404 there. The current public source of truth is the
embedded careers widget at `/jobs/embed2.php`, which renders all open jobs
as static HTML grouped by department:

    GET https://{slug}.bamboohr.com/jobs/embed2.php

Widget structure (one block per department, one `<li>` per job):

    <li id="bhrDepartmentID_{dept_id}" class="BambooHR-ATS-Department-Item">
      <div id="department_{dept_id}" class="BambooHR-ATS-Department-Header">
        {Department}
      </div>
      <ul class="BambooHR-ATS-Jobs-List">
        <li id="bhrPositionID_{job_id}" class="BambooHR-ATS-Jobs-Item">
          <a href="//{slug}.bamboohr.com/careers/{job_id}">{Title}</a>
          <span class="BambooHR-ATS-Location">{City, State}</span>
        </li>
      </ul>
    </li>

Tenants without open jobs return a 200 with an empty widget (~270 bytes).

For descriptions and the rest of the per-job fields the careers detail
page itself is JS-rendered, but the SPA hydrates from a clean public
JSON XHR at `/careers/{id}/detail`. We hit that directly to enrich each
job with description, employment type, compensation, posted date, and
canonical location.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

WIDGET_TEMPLATE = "https://{slug}.bamboohr.com/jobs/embed2.php"
DETAIL_TEMPLATE = "https://{slug}.bamboohr.com/careers/{id}/detail"
SHARE_URL_TEMPLATE = "https://{slug}.bamboohr.com/careers/{id}"

MAX_CONCURRENCY = 8

# Detail enrichment is best-effort: any non-2xx leaves the listing row
# intact instead of failing the run, so the Fetcher hands every error
# status back to us unmapped (and un-retried).
_DETAIL_HANDLED = frozenset(range(300, 600))

# BambooHR's ``employmentStatusLabel`` is freeform but tenants stick to
# a small set. Map to the shared employment-type enum.
_EMPLOYMENT_TYPE_MAP = {
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "full time": "FULL_TIME",
    "regular full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "part time": "PART_TIME",
    "regular part-time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "intern": "INTERN",
    "internship": "INTERN",
}

# Each department block is wrapped in a <li class="BambooHR-ATS-Department-Item">.
# Inside, the header div carries the department name and the <ul> holds jobs.
_DEPARTMENT_BLOCK_RE = re.compile(
    r'<li id="bhrDepartmentID_(?P<dept_id>\d+)"[^>]*'
    r'class="BambooHR-ATS-Department-Item"[^>]*>'
    r'(?P<body>.*?)'
    r'(?=<li id="bhrDepartmentID_|\Z)',
    re.DOTALL | re.IGNORECASE,
)
_DEPARTMENT_NAME_RE = re.compile(
    r'<div[^>]*class="BambooHR-ATS-Department-Header"[^>]*>\s*(?P<name>[^<]+?)\s*</div>',
    re.DOTALL | re.IGNORECASE,
)
_POSITION_RE = re.compile(
    r'<li id="bhrPositionID_(?P<id>\d+)"[^>]*'
    r'class="BambooHR-ATS-Jobs-Item"[^>]*>'
    r'(?P<body>.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)
_POSITION_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>\s*(?P<title>.*?)\s*</a>',
    re.DOTALL | re.IGNORECASE,
)
_POSITION_LOCATION_RE = re.compile(
    r'<span[^>]*class="BambooHR-ATS-Location"[^>]*>\s*(?P<loc>[^<]+?)\s*</span>',
    re.IGNORECASE,
)
# /careers/{id} detail page: description lives in a div with this class
_DETAIL_DESCRIPTION_RE = re.compile(
    r'<div[^>]*class="(?:[^"]*\b)?BambooHR-ATS-Description\b[^"]*"[^>]*>\s*(?P<body>.*?)\s*</div>\s*(?:<div|<footer|</body)',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.BAMBOOHR)
class BambooHRScraper(BaseScraper):
    """BambooHR scraper — `company_slug` is the tenant subdomain.

    Each job is enriched with the public ``/careers/{id}/detail`` JSON,
    which carries the full description, employment type, compensation,
    posted date, and canonical city/state/country. The enrichment runs
    in parallel under ``MAX_CONCURRENCY``."""

    ats = ATSType.BAMBOOHR

    default_headers: ClassVar[dict[str, str]] = {"User-Agent": "Mozilla/5.0"}

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(1)
                await self._enrich_one(fetch, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            widget_html = await fetch.get_text(
                WIDGET_TEMPLATE.format(slug=self.company_slug)
            )
            jobs = self._parse_widget(widget_html)
            if self.include_descriptions and jobs:
                await self._enrich_from_detail_api(fetch, jobs)
            return jobs

    def _parse_widget(self, html: str) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        # Walk department blocks so each job inherits its department name.
        # Some legacy tenants render jobs without a wrapping department —
        # handle those by also scanning the document tail.
        consumed_end = 0
        for dept_match in _DEPARTMENT_BLOCK_RE.finditer(html):
            consumed_end = dept_match.end()
            dept_body = dept_match.group("body")
            dept_name_match = _DEPARTMENT_NAME_RE.search(dept_body)
            dept_name = (
                _strip_tags(dept_name_match.group("name"))
                if dept_name_match
                else None
            )
            for position_match in _POSITION_RE.finditer(dept_body):
                job = self._parse_position(
                    position_match.group("id"),
                    position_match.group("body"),
                    department=dept_name,
                )
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)
        # Stragglers outside any department block.
        for position_match in _POSITION_RE.finditer(html, pos=consumed_end):
            job = self._parse_position(
                position_match.group("id"),
                position_match.group("body"),
                department=None,
            )
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _parse_position(
        self, ats_id: str, body: str, *, department: str | None
    ) -> Job | None:
        link = _POSITION_LINK_RE.search(body)
        if not link:
            return None
        title = _strip_tags(link.group("title"))
        if not title:
            return None
        href = link.group("href").strip()
        url = href if href.startswith("http") else (
            f"https:{href}" if href.startswith("//")
            else f"https://{self.company_slug}.bamboohr.com{href}"
        )
        loc_match = _POSITION_LOCATION_RE.search(body)
        location = loc_match.group("loc").strip() if loc_match else None
        return Job(
            url=url,
            title=title,
            company=self.company_slug,
            ats_type=ATSType.BAMBOOHR,
            ats_id=ats_id,
            location=location,
            department=department,
            posted_at=None,
            fetched_at=datetime.now(UTC),
        )

    async def _enrich_from_detail_api(
        self, fetch: Fetcher, jobs: list[Job]
    ) -> None:
        """Hydrate each job from `/careers/{id}/detail` JSON.

        Best-effort: failures (timeout, 404, JSON shape change) leave the
        listing-derived fields intact rather than crashing the run.
        """
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        await asyncio.gather(*(self._enrich_one(fetch, sem, j) for j in jobs))

    async def _enrich_one(
        self, fetch: Fetcher, sem: asyncio.Semaphore, job: Job,
    ) -> None:
        url = DETAIL_TEMPLATE.format(slug=self.company_slug, id=job.ats_id)
        async with sem:
            try:
                response = await fetch.request(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    handled=_DETAIL_HANDLED,
                )
            except ScraperError:
                return
        if response.status_code != 200:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        opening = (payload.get("result") or {}).get("jobOpening") or {}
        if not isinstance(opening, dict) or not opening:
            return
        _apply_opening_to_job(job, opening)


def _apply_opening_to_job(job: Job, opening: dict) -> None:
    """Hydrate ``job`` in place from a BambooHR ``jobOpening`` payload.

    Only fills fields that the listing pass left empty; canonical
    location from the detail page replaces the listing's terse
    "City, ST" snippet.
    """
    desc_html = opening.get("description")
    if isinstance(desc_html, str) and desc_html.strip():
        # ``description`` is HTML on BambooHR's side. Strip tags for
        # plain-text storage; cap at 25k chars to match the Job schema doc.
        job.description = _strip_tags(desc_html)[:25_000] or None

    emp_label = opening.get("employmentStatusLabel")
    if isinstance(emp_label, str) and emp_label.strip():
        norm = emp_label.strip().lower()
        for needle, mapped in _EMPLOYMENT_TYPE_MAP.items():
            if needle in norm:
                job.employment_type = mapped  # type: ignore[assignment]
                break

    compensation = opening.get("compensation")
    if (
        isinstance(compensation, str)
        and compensation.strip()
        and not job.salary_summary
    ):
        job.salary_summary = compensation.strip()

    date_posted = opening.get("datePosted")
    if isinstance(date_posted, str) and date_posted and not job.posted_at:
        with contextlib.suppress(ValueError):
            job.posted_at = datetime.fromisoformat(date_posted)

    location = opening.get("location") or {}
    if isinstance(location, dict):
        parts = [
            str(location.get(k) or "").strip()
            for k in ("city", "state", "addressCountry")
            if location.get(k)
        ]
        canonical = ", ".join(p for p in parts if p)
        if canonical:
            # Listing only has "City, ST" — replace with full canonical.
            job.location = canonical

    # ``locationType`` "1" / "remote" indicates a remote role; "0" =
    # on-site/hybrid (BambooHR doesn't distinguish hybrid).
    loc_type = opening.get("locationType")
    if loc_type is not None and job.is_remote is None:
        if str(loc_type).strip() in ("1", "2", "true") or (
            isinstance(loc_type, str) and "remote" in loc_type.lower()
        ):
            job.is_remote = True
        elif str(loc_type).strip() == "0":
            job.is_remote = False


def _strip_tags(text: str) -> str:
    """Tiny HTML→text helper. We control the markup (BambooHR widgets), so
    we don't need a full parser — strip tags, decode entities, collapse
    whitespace."""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
