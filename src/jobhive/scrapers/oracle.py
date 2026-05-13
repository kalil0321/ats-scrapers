"""Oracle HCM Cloud scraper.

⚠️  EXPERIMENTAL — the Oracle Recruiting Cloud REST API wraps job results in
a `requisitionList` envelope whose exact path varies per tenant. Title,
location, and posted-date field names also differ across versions. The basic
flow below works for many tenants but not all. For production-grade
reliability, fall back to the legacy `oracle/main.py` until 0.2.0.

Oracle Recruiting Cloud sites live at:
    https://{subdomain}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/...

The unauthenticated REST endpoint:
    GET {base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&limit=200&offset=0&finder=findReqs;siteNumber={site}

The companion detail endpoint
``/recruitingCEJobRequisitionDetails?finder=ById;Id={id}`` exposes the
full job description (``ExternalDescriptionStr``) plus the qualifications
and responsibilities sections. Detail enrichment is opt-in via
``JOBHIVE_ORACLE_FETCH_DESCRIPTIONS=1`` because Oracle's tenant universe
runs into hundreds of thousands of jobs — a per-job fan-out makes a
default scrape impractically slow.

Pass the full base URL (and optionally a site number) as the slug.
"""

from __future__ import annotations

import asyncio
import os
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

PAGE_LIMIT = 200
SITE_RE = re.compile(r"site_number=([^&]+)")
SITE_PATH_RE = re.compile(r"/sites/([^/?#]+)")
DEFAULT_SITE = "CX_1"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 8

_TAG_RE = re.compile(r"<[^>]+>")

# Oracle's ``WorkplaceTypeCode`` is a stable enum string — map to the
# canonical remote flag. ``ORA_HYBRID`` stays None (neither purely
# remote nor onsite).
_REMOTE_BY_CODE = {
    "ORA_REMOTE": True,
    "ORA_FULL_TIME_REMOTE": True,
    "ORA_ON_SITE": False,
    "ORA_ONSITE": False,
}

# WorkerType / JobType / JobSchedule labels → canonical employment-type
# enum. Oracle tenants pick from a freeform-ish vocabulary; match against
# the most common terms.
_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "co-op": "INTERN",
    "temporary": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "contractor": "CONTRACT",
    "contract": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "regular": "FULL_TIME",
    "permanent": "FULL_TIME",
}


def _normalize_oracle_target(raw_url: str) -> tuple[str, str]:
    """Return ``(host_root, site_number)`` for Oracle careers URLs.

    The tenant CSV stores public CandidateExperience URLs such as
    ``https://host/hcmUI/CandidateExperience/en/sites/CX_1``. Oracle's REST API
    lives at the host root, while the site number belongs in the finder string.
    Keep supporting the older ``https://host?site_number=CX_...`` form too.
    """
    match = SITE_RE.search(raw_url)
    site = match.group(1) if match else DEFAULT_SITE
    parsed = urlparse(raw_url)
    if parsed.scheme and parsed.netloc:
        path_site = SITE_PATH_RE.search(parsed.path)
        if not match and path_site:
            site = path_site.group(1)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/"), site
    return raw_url.split("?", 1)[0].rstrip("/"), site


@ScraperRegistry.register(ATSType.ORACLE)
class OracleScraper(BaseScraper):
    """Oracle scraper — `company_slug` is the full careers URL.

    Optionally append `?site_number=CX_xxxxx` to the URL to target a specific
    Oracle careers site within the tenant.
    """

    ats = ATSType.ORACLE

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        base, site = _normalize_oracle_target(self.company_slug)
        if not base.startswith(("http://", "https://")):
            raise ScraperError(
                f"Oracle slug must be a full URL (https://...oraclecloud.com), got {base!r}"
            )
        api = f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

        all_jobs: list[Job] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            # First call also tells us TotalJobsCount.
            first = await self._fetch_with_retry(client, api, base, site, offset=0)
            items, total = _unwrap(first)
            for it in items:
                job = self._parse_job(it, base, site)
                if job.ats_id and job.ats_id not in seen:
                    seen.add(job.ats_id)
                    all_jobs.append(job)
            if total is None or total <= len(items):
                return all_jobs

            # Paginate the rest. Use `len(items)` as the actual page size
            # (Oracle may return less than the requested limit on the first
            # page, e.g. 198 instead of 200).
            page_size = max(len(items), 1)
            offsets = list(range(page_size, total, page_size))
            for offset in offsets:
                payload = await self._fetch_with_retry(
                    client, api, base, site, offset=offset
                )
                page_items, _ = _unwrap(payload)
                if not page_items:
                    break
                for it in page_items:
                    job = self._parse_job(it, base, site)
                    if job.ats_id and job.ats_id not in seen:
                        seen.add(job.ats_id)
                        all_jobs.append(job)

            # Optional per-job detail enrichment. Disabled by default
            # because Oracle's tenant universe (a few hundred enterprise
            # tenants × hundreds-to-thousands of reqs each) makes a fan-out
            # scrape take hours. Toggle with
            # ``JOBHIVE_ORACLE_FETCH_DESCRIPTIONS=1`` when description
            # coverage is wanted.
            if all_jobs and os.getenv("JOBHIVE_ORACLE_FETCH_DESCRIPTIONS"):
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                detail_url = (
                    f"{base}/hcmRestApi/resources/latest/"
                    "recruitingCEJobRequisitionDetails"
                )
                await asyncio.gather(*(
                    self._enrich_detail(client, sem, detail_url, j)
                    for j in all_jobs
                ))
        return all_jobs

    async def _enrich_detail(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        detail_url: str,
        job: Job,
    ) -> None:
        if not job.ats_id or job.description:
            return
        async with sem:
            try:
                response = await client.get(
                    detail_url,
                    params={"finder": f"ById;Id={job.ats_id}", "onlyData": "true"},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        try:
            data = response.json()
        except ValueError:
            return
        items = data.get("items") or []
        if not items or not isinstance(items[0], dict):
            return
        detail = items[0]

        # Concatenate the three external sections — description /
        # qualifications / responsibilities — into a single plain-text
        # body, capped at 10kB.
        parts: list[str] = []
        for key in (
            "ExternalDescriptionStr",
            "ExternalResponsibilitiesStr",
            "ExternalQualificationsStr",
        ):
            v = detail.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(_strip_html(v))
        if parts:
            job.description = "\n\n".join(parts)[:10_000]

    async def _fetch_with_retry(
        self,
        client: httpx.AsyncClient,
        api: str,
        base: str,
        site: str,
        offset: int,
    ) -> dict[str, Any]:
        params = {
            "onlyData": "true",
            # Pagination params MUST live inside the `finder` string —
            # Oracle silently ignores top-level `limit`/`offset` and returns
            # a fixed 25 results from the first page when they're external.
            "finder": f"findReqs;siteNumber={site},limit={PAGE_LIMIT},offset={offset}",
            # Without `expand=requisitionList`, the response only contains
            # search-context metadata (facets, totalCount), not actual jobs.
            "expand": "requisitionList",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    api, params=params, headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Oracle fetch failed for {base} at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                raise CompanyNotFoundError(f"Oracle careers site not found: {base}")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Oracle ({base}) returned {response.status_code} at "
                        f"offset={offset} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Oracle returned {response.status_code} for {base} at offset={offset}"
            )
        raise ScraperError(f"Oracle ({base}) exhausted retries at offset={offset}")

    def _parse_job(self, item: dict[str, Any], base: str, site: str) -> Job:
        ats_id = str(item.get("Id") or item.get("RequisitionNumber") or "")
        company = urlparse(base).hostname or self.company_slug
        title = item.get("Title") or "Untitled"

        # Description from the listing's ``ShortDescriptionStr`` when
        # populated. Detail enrichment overwrites this with the longer
        # ``ExternalDescriptionStr`` when enabled.
        short_desc = item.get("ShortDescriptionStr")
        description = (
            _strip_html(short_desc)[:10_000]
            if isinstance(short_desc, str) and short_desc.strip()
            else None
        )

        # ``WorkplaceTypeCode`` is a stable enum (``ORA_REMOTE`` /
        # ``ORA_ON_SITE`` / ``ORA_HYBRID``); the textual ``WorkplaceType``
        # field is locale-dependent. Use the code.
        workplace_code = item.get("WorkplaceTypeCode")
        is_remote: bool | None = None
        if isinstance(workplace_code, str):
            is_remote = _REMOTE_BY_CODE.get(workplace_code.strip().upper())

        # Department — Oracle tenants populate one of these three; pick
        # the most specific available.
        department: str | None = None
        for key in ("Department", "Organization", "BusinessUnit"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                department = v.strip()
                break

        # Employment type — try a chain of fields, mapping each through
        # the freeform-vocabulary table.
        employment_type: str | None = None
        commitment: str | None = None
        for key in ("WorkerType", "JobType", "ContractType", "JobSchedule"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                norm = v.strip().lower()
                if commitment is None:
                    commitment = v.strip()
                if employment_type is None:
                    for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                        if needle in norm:
                            employment_type = mapped
                            break
                if employment_type is not None:
                    break

        # Requisition id — Oracle stores the human-readable req number
        # under multiple keys depending on the API version.
        req_raw = (
            item.get("RequisitionNumber")
            or item.get("RequisitionId")
            or item.get("ReqNumber")
        )
        requisition_id = str(req_raw).strip() if req_raw else None

        # Team — JobFamily is the closest analog when it's a string.
        team_raw = item.get("JobFamilyName") or item.get("JobFamily")
        team = (
            team_raw.strip() if isinstance(team_raw, str) and team_raw.strip()
            else None
        )

        raw: dict[str, Any] = {}
        for k in ("Category", "JobFamily", "JobFamilyName",
                  "JobFunction", "JobFunctionCode", "WorkLocation",
                  "WorkerType", "WorkerCategory", "WorkplaceTypeCode",
                  "ContractType", "JobSchedule", "JobShift", "JobType",
                  "Department", "Organization", "BusinessUnit",
                  "PrimaryLocationCountry", "GeographyId", "LegalEmployer"):
            v = item.get(k)
            if v not in (None, "", [], False):
                raw[k] = v

        return Job(
            url=item.get("ExternalURL")
            or f"{base}/?keyword=&mode=jobs&lang=en&site_number={site}#{ats_id}",
            title=title,
            company=company,
            ats_type=ATSType.ORACLE,
            ats_id=ats_id,
            location=item.get("PrimaryLocation"),
            is_remote=is_remote,
            department=department,
            team=team,
            employment_type=employment_type,
            commitment=commitment,
            description=description,
            requisition_id=requisition_id,
            posted_at=_parse_iso(item.get("PostedDate") or item.get("CreatedOn")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _unwrap(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    """Pull ``(requisitions, totalJobsCount)`` from Oracle's response.

    Oracle wraps the actual list at ``items[0].requisitionList`` and exposes
    the real total at ``items[0].TotalJobsCount``. Without ``expand=requisitionList``
    the inner list is missing entirely (only facet metadata returns).
    """
    items = payload.get("items") or []
    if not items or not isinstance(items[0], dict):
        return [], None
    item0 = items[0]
    reqs = item0.get("requisitionList")
    if not isinstance(reqs, list):
        return [], item0.get("TotalJobsCount")
    return reqs, item0.get("TotalJobsCount")


def _strip_html(text: str) -> str:
    import html as html_mod
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
