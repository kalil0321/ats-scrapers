"""Gem scraper.

Gem job boards live at ``https://jobs.gem.com/{slug}`` where ``{slug}`` is
the board ID (kebab-case, no spaces). Jobs are exposed via a public
GraphQL batch endpoint:

    POST https://jobs.gem.com/api/public/graphql/batch

The list query (``JobBoardList``) returns id/title/locations/department/
employmentType. Description, posted-date, requisition id, compensation
all live on the detail query (``ExternalJobPostingQuery``) — we batch up
to ``DETAIL_BATCH_SIZE`` of those into one POST.
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

BASE_URL = "https://jobs.gem.com"
GRAPHQL_URL = f"{BASE_URL}/api/public/graphql/batch"

# Pack this many ``ExternalJobPostingQuery`` ops into a single POST.
# Gem's batch endpoint comfortably handles 25; cap conservatively to
# limit per-request payload size and keep retries cheap.
DETAIL_BATCH_SIZE = 20

JOB_BOARD_LIST_QUERY = """
query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      id
      extId
      title
      locations { id name city isoCountry isRemote extId __typename }
      job {
        id
        department { id name extId __typename }
        locationType
        employmentType
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

# The detail query is what the SPA fires per-job-page navigation. We
# trim the upstream form-template / question fields we don't need —
# only the job-side fields are kept.
EXTERNAL_JOB_POSTING_QUERY = """
query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    extId
    startDateTs
    firstPublishedTsSec
    companyUrl
    locations {
      id
      extId
      name
      city
      isoCountry
      isRemote
      __typename
    }
    job {
      id
      locationType
      employmentType
      requisitionId
      teamDisplayName
      department { id extId name __typename }
      __typename
    }
    compensationHtml
    __typename
  }
}
"""

_TAG_RE = re.compile(r"<[^>]+>")


_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "FULLTIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "PARTTIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "TEMP": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}


@ScraperRegistry.register(ATSType.GEM)
class GemScraper(BaseScraper):
    """Gem scraper — `company_slug` is the board slug shown in the job-board
    URL (e.g. for `https://jobs.gem.com/accel`, pass `accel`)."""

    ats = ATSType.GEM

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            postings = await self._fetch_list(client)
            jobs = [self._parse_job(item) for item in postings]
            if jobs:
                await self._enrich_with_details(client, jobs, postings)
            return jobs

    async def _fetch_list(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        payload = [
            {
                "operationName": "JobBoardList",
                "variables": {"boardId": self.company_slug},
                "query": JOB_BOARD_LIST_QUERY,
            }
        ]
        try:
            response = await client.post(
                GRAPHQL_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"Gem fetch failed for {self.company_slug}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise ScraperError(
                f"Gem returned {response.status_code} for {self.company_slug}"
            )
        batch = response.json()
        if not batch:
            return []
        result = batch[0] or {}
        if result.get("errors"):
            raise CompanyNotFoundError(
                f"Gem board not found: {self.company_slug} "
                f"({result['errors'][0].get('message')})"
            )
        data = (result.get("data") or {}).get("oatsExternalJobPostings") or {}
        return [
            p for p in (data.get("jobPostings") or [])
            if isinstance(p, dict)
        ]

    async def _enrich_with_details(
        self,
        client: httpx.AsyncClient,
        jobs: list[Job],
        postings: list[dict[str, Any]],
    ) -> None:
        """Fan out detail-query batches; hydrate ``description``,
        ``posted_at``, ``requisition_id``, ``team``, ``salary_summary``,
        ``employment_type`` (canonical enum) and ``commitment``."""
        ext_ids = [
            (j, p.get("extId") or p.get("id"))
            for j, p in zip(jobs, postings, strict=True)
            if (p.get("extId") or p.get("id"))
        ]
        if not ext_ids:
            return

        # Batch into groups of DETAIL_BATCH_SIZE; one POST per batch
        # carrying that many ExternalJobPostingQuery operations.
        async def fetch_batch(batch: list[tuple[Job, str]]) -> None:
            payload = [
                {
                    "operationName": "ExternalJobPostingQuery",
                    "variables": {"boardId": self.company_slug, "extId": ext_id},
                    "query": EXTERNAL_JOB_POSTING_QUERY,
                }
                for _, ext_id in batch
            ]
            try:
                response = await client.post(
                    GRAPHQL_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError:
                return
            if response.status_code != 200:
                return
            try:
                results = response.json()
            except ValueError:
                return
            if not isinstance(results, list):
                return
            for (job, _ext_id), result in zip(batch, results, strict=False):
                if not isinstance(result, dict) or result.get("errors"):
                    continue
                detail = (result.get("data") or {}).get("oatsExternalJobPosting")
                if isinstance(detail, dict):
                    _apply_detail_to_job(job, detail)

        batches = [
            ext_ids[i:i + DETAIL_BATCH_SIZE]
            for i in range(0, len(ext_ids), DETAIL_BATCH_SIZE)
        ]
        await asyncio.gather(*(fetch_batch(b) for b in batches))

    def _parse_job(self, item: dict[str, Any]) -> Job:
        ext_id = item.get("extId") or item["id"]

        job_obj = item.get("job") or {}
        emp_raw = job_obj.get("employmentType") if isinstance(job_obj, dict) else None
        employment_type = (
            _EMPLOYMENT_TYPE_MAP.get((emp_raw or "").upper())
            if isinstance(emp_raw, str) else None
        )

        # Department is nested in ``job.department.name``.
        dept = None
        dept_obj = job_obj.get("department") if isinstance(job_obj, dict) else None
        if isinstance(dept_obj, dict):
            dept = dept_obj.get("name")

        raw: dict[str, Any] = {}
        for k in ("locationType", "employmentType"):
            v = job_obj.get(k) if isinstance(job_obj, dict) else None
            if v:
                raw[k] = v

        return Job(
            url=f"{BASE_URL}/{self.company_slug}/{ext_id}",
            title=item["title"],
            company=self.company_slug,
            ats_type=ATSType.GEM,
            ats_id=str(ext_id),
            location=_extract_location(item.get("locations") or []),
            is_remote=_extract_is_remote(item.get("locations") or []),
            department=dept if isinstance(dept, str) else None,
            employment_type=employment_type,
            commitment=emp_raw if isinstance(emp_raw, str) else None,
            posted_at=None,  # Filled by detail enrichment.
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _apply_detail_to_job(job: Job, detail: dict[str, Any]) -> None:
    """Hydrate ``job`` from an ``oatsExternalJobPosting`` detail payload.

    Best-effort: the listing-derived row stays usable even if any single
    field can't be parsed.
    """
    desc_html = detail.get("descriptionHtml")
    if isinstance(desc_html, str) and desc_html.strip():
        job.description = _strip_html(desc_html)[:12_000] or None

    # Posted date — Gem ships ``firstPublishedTsSec`` (epoch seconds) and
    # ``startDateTs`` (epoch seconds, *future* go-live). Prefer the
    # publish timestamp; fall through to startDate if the role hasn't
    # gone public yet.
    for key in ("firstPublishedTsSec", "startDateTs"):
        ts = detail.get(key)
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                job.posted_at = datetime.fromtimestamp(ts)
                break
            except (OSError, ValueError):
                continue

    job_obj = detail.get("job") or {}
    if isinstance(job_obj, dict):
        req = job_obj.get("requisitionId")
        if isinstance(req, str) and req.strip() and not job.requisition_id:
            job.requisition_id = req.strip()
        team = job_obj.get("teamDisplayName")
        if isinstance(team, str) and team.strip() and not job.team:
            job.team = team.strip()

    comp_html = detail.get("compensationHtml")
    if isinstance(comp_html, str) and comp_html.strip() and not job.salary_summary:
        job.salary_summary = _strip_html(comp_html)[:500] or None


def _extract_location(locations: list[dict[str, Any]]) -> str | None:
    if not locations:
        return None
    first = locations[0]
    parts = [first.get("city"), first.get("isoCountry")]
    joined = ", ".join(p for p in parts if p)
    return joined or first.get("name")


def _extract_is_remote(locations: list[dict[str, Any]]) -> bool | None:
    """Return True if any of the listed locations is flagged remote.

    Gem stores ``isRemote`` per-location; a job offered in multiple
    cities + remote should carry ``is_remote=True`` so remote-search
    queries hit it.
    """
    if not locations:
        return None
    for loc in locations:
        if isinstance(loc, dict) and loc.get("isRemote") is True:
            return True
    return False if any(isinstance(loc, dict) for loc in locations) else None


def _strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
