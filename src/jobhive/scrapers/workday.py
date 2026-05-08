"""Workday scraper.

Workday career sites live at the pattern:
    https://{company}.{instance}.myworkdayjobs.com/{site}

The corresponding (undocumented but stable) API:
    POST https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}/jobs

We accept the full careers URL as `company_slug` and parse out the three
components.

Pagination strategy:

The API caps ``limit`` at 20 hits per page (>20 returns 400). It also
caps the **reported total** at 2,000 per query. Past offset=2,000 the
API silently loops back to the first page — so a naïve scraper can never
collect more than 2K jobs from any one query no matter how it paginates.

For tenants with >2K jobs (e.g. Accenture has ~61K, Dollar Tree ~22K),
we subdivide by the ``jobFamilyGroup`` facet ("Area of Work"). Each
filtered query has its own ≤2K cap, and the union covers the full set.

The ``facets`` field in every response carries each value's true ``count``
— so we can plan the subdivision optimally without extra probes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

URL_PATTERN = re.compile(
    r"^https://(?P<company>[^.]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/(?P<site>[^/?#]+)"
)
PAGE_LIMIT = 20  # Workday hard-caps `limit` at 20 — higher returns 400.

# Workday's ``timeType`` is a stable enum: "Full time" / "Part time".
_TIMETYPE_TO_EMPLOYMENT_TYPE = {
    "full time": "FULL_TIME",
    "full_time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "part time": "PART_TIME",
    "part_time": "PART_TIME",
    "parttime": "PART_TIME",
    "fixed term": "CONTRACT",
    "contract": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}

# ``remoteType`` is a freeform string Workday tenants populate
# inconsistently. Map the obvious values.
_REMOTE_TYPE_PATTERNS = {
    "remote": True,
    "fully remote": True,
    "100% remote": True,
    "work from home": True,
    "telecommute": True,
    "telework": True,
    "on-site": False,
    "onsite": False,
    "in office": False,
    "in-office": False,
    "office": False,
    # ``flexible``, ``hybrid`` etc. stay None — neither purely remote nor onsite.
}
QUERY_TOTAL_CAP = 2000  # On capped tenants, total is reported as exactly 2000
                       # and pagination past offset=2000 wraps to page 1.
                       # Detection: total == QUERY_TOTAL_CAP triggers subdivision.
                       # Tenants with no cap (Dollar Tree, ~22K) report the real
                       # total and paginate cleanly.
MAX_SUBDIVISION_DEPTH = 4  # Recursion bound — Accenture needs depth 3 to fully
                          # cover Software Engineering (32K jobs). Depth 4 is a
                          # paranoid ceiling.
MAX_CONCURRENCY = 10
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

# Facets we'll try as subdivision dimensions, in priority order. After
# `jobFamilyGroup` (which usually covers level 1 well), `timeType`
# partitions further into Full/Part-time. `workerSubType` (Skills) is
# multi-tag — its sum exceeds total — but each query still returns valid
# subsets and dedup absorbs the overlap, so it's our level-3 fallback for
# tenants like Accenture's Software Engineering (32K jobs in one area).
_SUBDIVISION_FACETS = ("jobFamilyGroup", "timeType", "locations", "workerSubType")


@ScraperRegistry.register(ATSType.WORKDAY)
class WorkdayScraper(BaseScraper):
    """Workday scraper — `company_slug` must be the full careers URL."""

    ats = ATSType.WORKDAY

    def fetch(self) -> list[Job]:
        match = URL_PATTERN.match(self.company_slug.rstrip("/"))
        if not match:
            raise ScraperError(
                f"Workday URL must look like https://{{co}}.wdN.myworkdayjobs.com/{{site}} — "
                f"got {self.company_slug!r}"
            )
        company = match.group("company")
        instance = match.group("instance")
        site = match.group("site")
        api = f"https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}/jobs"
        base = self.company_slug.split("/wday/")[0].rstrip("/")

        return asyncio.run(self._fetch_async(api, base, company))

    async def _fetch_async(self, api: str, base: str, company: str) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            seen: set[str] = set()
            all_jobs: list[Job] = []

            def absorb(postings: list[dict[str, Any]]) -> None:
                for posting in postings:
                    job = self._parse_job(posting, base, company)
                    key = job.ats_id or str(job.url)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_jobs.append(job)

            await self._exhaust_query(
                client, api, sem,
                applied_facets={}, absorb=absorb, depth=0,
            )
        return all_jobs

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient,
        api: str,
        sem: asyncio.Semaphore,
        *,
        applied_facets: dict[str, list[str]],
        absorb,
        depth: int,
    ) -> None:
        """Recursively exhaust the given filter combination.

        - If total != cap → paginate normally.
        - If total == cap and we have unused facets → subdivide.
        - Otherwise (cap reached, no more facets, or max depth) → take
          what we can from this capped query (up to 2000 jobs) and stop.
        """
        first = await self._request(client, api, sem, applied_facets=applied_facets, offset=0)
        if first is None:
            return
        total = int(first.get("total", 0))
        absorb(first.get("jobPostings") or [])
        if total <= PAGE_LIMIT:
            return

        is_capped = total == QUERY_TOTAL_CAP
        if not is_capped:
            await self._fan_out_pages(
                client, api, sem,
                applied_facets=applied_facets, total=total, absorb=absorb,
            )
            return

        # total is capped at 2000. Try to subdivide further.
        if depth >= MAX_SUBDIVISION_DEPTH:
            # Recursion bound — accept the capped 2000 from this query.
            await self._fan_out_pages(
                client, api, sem,
                applied_facets=applied_facets, total=total, absorb=absorb,
            )
            return

        facet = _pick_subdivision_facet(
            first.get("facets") or [],
            already_applied=set(applied_facets.keys()),
        )
        if facet is None:
            # No more partitioning facets available — take the capped 2000.
            await self._fan_out_pages(
                client, api, sem,
                applied_facets=applied_facets, total=total, absorb=absorb,
            )
            return

        param, values = facet

        async def child(value_id: str) -> None:
            child_filters = {**applied_facets, param: [value_id]}
            await self._exhaust_query(
                client, api, sem,
                applied_facets=child_filters, absorb=absorb, depth=depth + 1,
            )

        await asyncio.gather(*(child(v_id) for v_id, _ in values))

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient,
        api: str,
        sem: asyncio.Semaphore,
        *,
        applied_facets: dict[str, list[str]],
        total: int,
        absorb,
    ) -> None:
        """Fan out offsets [PAGE_LIMIT, total) under the shared semaphore."""
        offsets = list(range(PAGE_LIMIT, total, PAGE_LIMIT))

        async def fetch_one(offset: int) -> list[dict[str, Any]]:
            payload = await self._request(
                client, api, sem, applied_facets=applied_facets, offset=offset
            )
            return (payload or {}).get("jobPostings") or []

        results = await asyncio.gather(*(fetch_one(o) for o in offsets))
        for batch in results:
            absorb(batch)

    async def _request(
        self,
        client: httpx.AsyncClient,
        api: str,
        sem: asyncio.Semaphore,
        *,
        applied_facets: dict[str, list[str]],
        offset: int,
    ) -> dict[str, Any] | None:
        body = {
            "appliedFacets": applied_facets,
            "limit": PAGE_LIMIT,
            "offset": offset,
            "searchText": "",
        }
        # Workday 403s when we burst — also retryable. 401 means CSRF-protected
        # tenant (some need an init handshake; we don't currently support those).
        retryable_statuses = {403, 429, 502, 503, 504}
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            async with sem:
                try:
                    response = await client.post(
                        api, json=body, headers={"Content-Type": "application/json"}
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    await asyncio.sleep(RETRY_BACKOFF ** attempt)
                    continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Workday site not found: {self.company_slug}"
                )
            if response.status_code == 200:
                return response.json()
            if response.status_code in retryable_statuses:
                # Exponential backoff respects Retry-After when present.
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BACKOFF ** attempt
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Workday returned {response.status_code} for {self.company_slug} "
                f"(offset={offset})"
            )
        raise ScraperError(
            f"Workday gave up after {MAX_RETRIES} retries at offset={offset}: {last_exc}"
        )

    def _parse_job(self, item: dict[str, Any], base_url: str, company: str) -> Job:
        external_path = item.get("externalPath", "") or ""
        bullet_req = (item.get("bulletFields") or [None])[0]
        ats_id = bullet_req or external_path.rsplit("/", 1)[-1] or "unknown"
        # bulletFields[0] is canonically the requisition id on Workday tenants
        # that surface it (Accenture R-…, Salesforce JR-…). Same value across
        # mirrors (Eightfold wrappers).
        requisition_id = bullet_req if bullet_req and bullet_req != ats_id else None
        if bullet_req:
            requisition_id = bullet_req

        # ``timeType`` ("Full time" / "Part time") is the canonical
        # Workday signal; map to our employment-type enum.
        time_type = item.get("timeType")
        commitment = (
            time_type.strip()
            if isinstance(time_type, str) and time_type.strip()
            else None
        )
        employment_type: str | None = None
        if commitment:
            norm = commitment.strip().lower().replace("-", " ")
            employment_type = _TIMETYPE_TO_EMPLOYMENT_TYPE.get(norm)
            if not employment_type:
                for needle, mapped in _TIMETYPE_TO_EMPLOYMENT_TYPE.items():
                    if needle in norm:
                        employment_type = mapped
                        break

        # ``remoteType`` is freeform — Workday tenants populate things
        # like "Remote", "Fully Remote", "Hybrid", "On-site", or
        # nothing. Map the unambiguous extremes; hybrid stays None.
        remote_type = item.get("remoteType")
        is_remote: bool | None = None
        if isinstance(remote_type, str) and remote_type.strip():
            norm = remote_type.strip().lower()
            if norm in _REMOTE_TYPE_PATTERNS:
                is_remote = _REMOTE_TYPE_PATTERNS[norm]
            elif "remote" in norm and "hybrid" not in norm:
                is_remote = True
            elif "hybrid" not in norm and (
                "site" in norm or "office" in norm
            ):
                is_remote = False

        # Department from ``jobFamilyGroup`` when populated (it's the
        # highest-level facet Workday surfaces in the listing — also
        # the same axis we subdivide on).
        department: str | None = None
        jfg = item.get("jobFamilyGroup")
        if isinstance(jfg, str) and jfg.strip():
            department = jfg.strip()

        raw: dict[str, Any] = {}
        if item.get("bulletFields"):
            raw["bullet_fields"] = item["bulletFields"]
        for k in ("locations", "timeType", "jobFamilyGroup", "remoteType"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=f"{base_url}{external_path}" if external_path else base_url,
            title=item.get("title") or "Untitled",
            company=company,
            ats_type=ATSType.WORKDAY,
            ats_id=ats_id,
            location=item.get("locationsText"),
            is_remote=is_remote,
            employment_type=employment_type,
            commitment=commitment,
            department=department,
            requisition_id=requisition_id,
            posted_at=_parse_workday_date(item.get("postedOn")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _parse_workday_date(value: str | None) -> datetime | None:
    """Workday's `postedOn` is a relative string like 'Posted 30+ Days Ago'."""
    if not value:
        return None
    return None  # Relative; absolute date requires fetching the per-job detail.


def _pick_subdivision_facet(
    facets: list[dict[str, Any]],
    *,
    already_applied: set[str],
) -> tuple[str, list[tuple[str, int]]] | None:
    """Return ``(param, [(value_id, value_count), ...])`` for the best facet
    to subdivide further on, or ``None`` if nothing useful remains.

    Skips facets that are already in ``already_applied`` — reusing the same
    facet would just hit the cap again. Tries the priority list
    (``jobFamilyGroup`` → ``timeType`` → ``locations``) first, then falls
    back to the highest-cardinality remaining facet (typically
    ``workerSubType``/Skills, which is multi-tag — dedup absorbs the
    overlap).
    """
    by_param: dict[str, list[tuple[str, int]]] = {}
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        param = facet.get("facetParameter")
        values = facet.get("values") or []
        if not param or param in already_applied or len(values) < 2:
            continue
        items = [
            (v.get("id"), int(v.get("count") or 0))
            for v in values
            if isinstance(v, dict) and v.get("id") and v.get("count", 0) > 0
        ]
        if items:
            by_param[param] = items

    for preferred in _SUBDIVISION_FACETS:
        if preferred in by_param:
            return preferred, by_param[preferred]
    if by_param:
        param, values = max(by_param.items(), key=lambda kv: len(kv[1]))
        return param, values
    return None
