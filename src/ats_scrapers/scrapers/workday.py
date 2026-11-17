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

For tenants with >2K jobs (e.g. Accenture), we subdivide by the
``jobFamilyGroup`` facet ("Area of Work"), and a filtered query still at
the cap by further facets (time type, country, city, skills). Each
filtered query has its own ≤2K cap, and the union covers the full set.

The ``facets`` field in every response carries each value's true ``count``
— so we can plan the subdivision optimally without extra probes.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r"^https://(?P<company>[^.]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/(?P<site>[^/?#]+)"
)
# The counter Workday adds to the path of a requisition posted again.
_POSTING_COUNTER_RE = re.compile(r"-\d{1,3}$")
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
# Recursion bound. Each level applies one more facet, so the facets a tenant
# offers end the recursion first; Accenture's largest area needs five.
MAX_SUBDIVISION_DEPTH = 8
MAX_CONCURRENCY = 10
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
MAX_RETRY_DELAY = 30.0

# When a Workday job spans multiple offices, the search endpoint returns a
# rollup string in ``locationsText`` like "2 Locations" / "5 Locations"
# instead of the actual list — the underlying ``locations`` array is not
# included in the search payload. ``_enrich_details`` detects these and
# overwrites the placeholder with the real city list from
# ``jobPostingInfo.location`` + ``additionalLocations``.
_LOCATION_ROLLUP_RE = re.compile(r"^\s*\d+\s+Locations?\s*$", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# Facets we'll try as subdivision dimensions, in priority order. After
# `jobFamilyGroup` (which usually covers level 1 well), `timeType`
# partitions further into Full/Part-time, then the location facets, which
# Workday nests under `locationMainGroup`. `workerSubType` (Skills) is
# multi-tag (its sum exceeds total) but each query still returns valid
# subsets and dedup absorbs the overlap, so it's the last resort.
_SUBDIVISION_FACETS = (
    "jobFamilyGroup",
    "timeType",
    "locationCountry",
    "Location_Country",
    "locationRegionStateProvince",
    "Location_Region_State_Province",
    "locations",
    "workerSubType",
)
# `distance` lists search radii around a typed-in place, one posting each:
# it never splits the postings.
_IGNORED_FACETS = frozenset({"distance"})
# Facets that give each posting at most one value: their counts add up to
# at most the search's real size, while the reported total stops at 2,000.
_SINGLE_VALUE_FACETS = ("jobFamilyGroup", "timeType", "workerSubType")


@ScraperRegistry.register(ATSType.WORKDAY)
class WorkdayScraper(BaseScraper):
    """Workday scraper — `company_slug` must be the full careers URL."""

    ats = ATSType.WORKDAY

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_fetch_seconds: float | None = None,
        company_name: str | None = None,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.max_fetch_seconds = max_fetch_seconds
        self.company_name = (
            company_name.strip()
            if company_name and company_name.strip()
            else None
        )
        self._deadline: float | None = None

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> WorkdayScraper:
        """Build a scraper from the full public careers URL.

        Workday has no bare-slug addressing: ``url`` must be the complete
        careers URL in the shape
        ``https://{company}.{instance}.myworkdayjobs.com/{site}`` (e.g.
        ``https://accenture.wd103.myworkdayjobs.com/accenturecareers``) —
        the tenant, ``wdN`` instance, and site name are all load-bearing
        and are parsed back out of the URL at fetch time. This constructor
        makes that contract explicit; it simply delegates to the normal
        constructor (``company_slug`` *is* the URL), and ``kwargs`` are
        forwarded unchanged (``timeout``, ``max_fetch_seconds``,
        ``company_name``, ``include_descriptions``, ``proxy``).
        """
        return cls(url, **kwargs)

    async def afetch(self) -> list[Job]:
        match = URL_PATTERN.match(self.company_slug.rstrip("/"))
        if not match:
            raise ScraperError(
                f"Workday URL must look like https://{{co}}.wdN.myworkdayjobs.com/{{site}} — "
                f"got {self.company_slug!r}"
            )
        company = match.group("company")
        display_company = self.company_name or company
        instance = match.group("instance")
        site = match.group("site")
        api = f"https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}/jobs"
        # The detail endpoint shares the cxs prefix but takes a job-relative
        # path: GET /wday/cxs/{co}/{site}{externalPath}.
        detail_prefix = f"https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}"
        base = self.company_slug.split("/wday/")[0].rstrip("/")

        self._deadline = (
            time.monotonic() + self.max_fetch_seconds
            if self.max_fetch_seconds
            else None
        )
        try:
            return await self._fetch_all(
                api,
                base,
                display_company,
                detail_prefix,
            )
        finally:
            self._deadline = None

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise ScraperError(
                f"Workday tenant exceeded max_fetch_seconds="
                f"{self.max_fetch_seconds:g}: {self.company_slug}"
            )

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        match = URL_PATTERN.match(self.company_slug.rstrip("/"))
        if not match:
            return None
        company = match.group("company")
        instance = match.group("instance")
        site = match.group("site")
        detail_prefix = f"https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}"
        jobs = [job.model_copy()]

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, proxy=self.proxy,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_details(client, sem, detail_prefix, jobs)
            return jobs[0].description

        return self._run_sync(run())

    async def _fetch_all(
        self,
        api: str,
        base: str,
        display_company: str,
        detail_prefix: str,
    ) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=self.proxy
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            seen: set[str] = set()
            all_jobs: list[Job] = []

            def absorb(postings: list[dict[str, Any]]) -> None:
                for posting in postings:
                    job = self._parse_job(
                        posting,
                        base,
                        display_company,
                    )
                    if job is None:
                        continue
                    key = job.ats_id or str(job.url)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_jobs.append(job)

            await self._exhaust_query(
                client, api, sem,
                applied_facets={}, absorb=absorb, depth=0,
            )

            if self.include_descriptions:
                await self._enrich_details(
                    client, sem, detail_prefix, all_jobs,
                )
        return all_jobs

    async def _enrich_details(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        detail_prefix: str,
        jobs: list[Job],
    ) -> None:
        """Best-effort per-job detail hydration.

        The search endpoint intentionally omits full posting bodies, so rows
        built only from ``jobPostings`` have ``description=None``. The detail
        endpoint exposes ``jobPostingInfo.jobDescription`` for the same
        ``externalPath``. It also exposes real locations for search rows whose
        ``locationsText`` is only a rollup string like ``"2 Locations"``.

        Detail failures stay non-fatal: a blocked/moved single posting should
        not discard the listing row or the rest of the tenant.
        """
        targets = [
            (i, j) for i, j in enumerate(jobs)
            if (j.raw or {}).get("externalPath") or _external_path(j.url)
        ]
        if not targets:
            return

        async def resolve(i: int, job: Job) -> None:
            external_path = (job.raw or {}).get("externalPath") or _external_path(job.url)
            if not external_path:
                return
            url = f"{detail_prefix}{external_path}"
            async with sem:
                try:
                    response = await client.get(
                        url,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                    )
                except httpx.HTTPError:
                    return
            if response.status_code != 200:
                return
            try:
                payload = response.json()
            except ValueError:
                return
            jpi = payload.get("jobPostingInfo") or {}
            updates: dict[str, Any] = {}

            description = _extract_description(jpi)
            if description and not job.description:
                updates["description"] = description[:25_000]

            if isinstance(job.location, str) and _LOCATION_ROLLUP_RE.match(job.location):
                primary = jpi.get("location")
                additional = jpi.get("additionalLocations") or []
                resolved = _format_locations(primary, additional)
                if resolved:
                    updates["location"] = resolved

            # Capture posting dates from the detail payload. Workday sends
            # bare 'YYYY-MM-DD' strings with no time or timezone. Merge into
            # raw rather than replacing it, so listing-sourced keys
            # (externalPath, bullet_fields) survive the model_copy update below.
            start_date = jpi.get("startDate")
            end_date = jpi.get("endDate")
            if start_date or end_date:
                merged_raw = dict(job.raw or {})
                if start_date:
                    merged_raw["startDate"] = start_date
                if end_date:
                    merged_raw["endDate"] = end_date
                updates["raw"] = merged_raw

            if updates:
                jobs[i] = job.model_copy(update=updates)

        await asyncio.gather(*(resolve(i, j) for i, j in targets))

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient,
        api: str,
        sem: asyncio.Semaphore,
        *,
        applied_facets: dict[str, list[str]],
        absorb,
        depth: int,
        size: int | None = None,
    ) -> None:
        """Recursively exhaust the given filter combination.

        ``size`` is the job count the parent search gave this filter value.

        - If total != cap → paginate normally.
        - If total == cap and we have unused facets → subdivide.
        - Otherwise (cap reached, no more facets, or max depth) → take
          what we can from this capped query (up to 2000 jobs) and stop.
        """
        self._check_deadline()
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

        # total is capped at 2000. Try to subdivide further. The capped total
        # is a floor for the size when the parent gave none (the board root).
        facet = (
            _pick_subdivision_facet(
                first.get("facets") or [],
                already_applied=set(applied_facets.keys()),
                size=max(size or 0, total),
            )
            if depth < MAX_SUBDIVISION_DEPTH
            else None
        )
        if facet is None:
            # Nothing left to split on: only the first 2000 are reachable.
            logger.warning(
                "Workday (%s): a search filtered on %s still holds more than "
                "%d jobs and cannot be split further; only %d are read",
                self.company_slug, sorted(applied_facets) or "nothing",
                QUERY_TOTAL_CAP, QUERY_TOTAL_CAP,
            )
            await self._fan_out_pages(
                client, api, sem,
                applied_facets=applied_facets, total=total, absorb=absorb,
            )
            return

        param, values = facet

        async def child(value_id: str, count: int) -> None:
            self._check_deadline()
            child_filters = {**applied_facets, param: [value_id]}
            await self._exhaust_query(
                client, api, sem,
                applied_facets=child_filters, absorb=absorb, depth=depth + 1,
                size=count,
            )

        await asyncio.gather(*(child(v_id, count) for v_id, count in values))

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
        self._check_deadline()
        offsets = list(range(PAGE_LIMIT, total, PAGE_LIMIT))

        async def fetch_one(offset: int) -> list[dict[str, Any]]:
            self._check_deadline()
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
        last_error = "no response"
        for attempt in range(MAX_RETRIES):
            self._check_deadline()
            async with sem:
                try:
                    response = await client.post(
                        api, json=body, headers={"Content-Type": "application/json"}
                    )
                except httpx.HTTPError as exc:
                    last_error = (
                        f"{type(exc).__name__}: {exc}" if str(exc)
                        else type(exc).__name__
                    )
                    await asyncio.sleep(min(MAX_RETRY_DELAY, RETRY_BACKOFF ** attempt))
                    continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Workday site not found: {self.company_slug}"
                )
            if offset == 0 and not applied_facets:
                gone = _tenant_gone(response)
                if gone:
                    raise CompanyNotFoundError(
                        f"Workday tenant {self.company_slug} {gone}"
                    )
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    content_type = response.headers.get(
                        "Content-Type",
                        "unknown",
                    )
                    last_error = (
                        "Workday returned a non-JSON success response for "
                        f"{self.company_slug} (offset={offset}, "
                        f"content-type={content_type})"
                    )
                    await asyncio.sleep(
                        min(MAX_RETRY_DELAY, RETRY_BACKOFF ** attempt)
                    )
                    continue
                if not isinstance(payload, dict):
                    last_error = (
                        "Workday returned an unexpected JSON payload for "
                        f"{self.company_slug} (offset={offset})"
                    )
                    await asyncio.sleep(
                        min(MAX_RETRY_DELAY, RETRY_BACKOFF ** attempt)
                    )
                    continue
                return payload
            if response.status_code in retryable_statuses:
                last_error = f"HTTP {response.status_code}"
                # Exponential backoff respects Retry-After when present.
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BACKOFF ** attempt
                )
                delay = min(MAX_RETRY_DELAY, delay)
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Workday returned {response.status_code} for {self.company_slug} "
                f"(offset={offset})"
            )
        raise ScraperError(
            f"Workday gave up after {MAX_RETRIES} retries at offset={offset}: {last_error}"
        )

    def _parse_job(
        self,
        item: dict[str, Any],
        base_url: str,
        company: str,
    ) -> Job | None:
        external_path = item.get("externalPath", "") or ""
        requisition_id, canonical_path = _requisition(
            item.get("bulletFields"),
            external_path,
        )
        ats_id = _job_identity(base_url, canonical_path)
        if ats_id is None:
            return None

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
        # Stash externalPath so the post-scrape rollup-resolver can build
        # the detail URL without re-parsing the job url.
        if external_path:
            raw["externalPath"] = external_path

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
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _parse_workday_date(value: str | None) -> datetime | None:
    """Workday's `postedOn` is a relative string like 'Posted 30+ Days Ago'."""
    if not value:
        return None
    return None  # Relative; absolute date requires fetching the per-job detail.


def _external_path(url: object) -> str | None:
    """Extract the Workday external path from a full job URL.

    Workday URLs are ``{base}/{site}{externalPath}`` — externalPath
    starts with ``/job/...``. We grep for that prefix to stay schema-
    independent across tenants.
    """
    if not url:
        return None
    s = str(url)
    idx = s.find("/job/")
    return s[idx:] if idx >= 0 else None


def _job_identity(base_url: str, canonical_path: object) -> str | None:
    """The posting's identity: the tenant's host and the posting's path.

    The career site is left out. A tenant often lists one posting on
    several of its sites, and the sites then give it the same path, apart
    from a counter that :func:`_requisition` removes.
    """
    if not isinstance(canonical_path, str) or not canonical_path.strip():
        return None
    path = canonical_path.strip().rstrip("/")
    if not path.startswith("/job/"):
        return None
    host = urlsplit(base_url).hostname
    if not host:
        return None
    return f"{host.casefold()}{path}"


def _tenant_gone(response: Any) -> str | None:
    """Why the first search shows the tenant is not served from this host.

    A tenant that moved to another data center answers 410
    ``ERR_TENANT_MIGRATED``. A host that serves no such tenant answers 422
    ``HTTP_422``; Workday answers a malformed search with 400 instead.
    """
    if response.status_code not in (410, 422):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    if response.status_code == 410 and code == "ERR_TENANT_MIGRATED":
        return "moved to another Workday data center"
    if response.status_code == 422 and code == "HTTP_422":
        return "is not served from this host: it moved or left Workday"
    return None


def _requisition(
    bullet_fields: object,
    external_path: object,
) -> tuple[str | None, object]:
    """The requisition id and the path without its posting counter.

    Workday ends a posting's path with ``{title}_{requisition}``. When a
    requisition is posted more than once (on a second career site, most
    often), the later postings get a counter: ``..._R123-1``,
    ``..._R123-2``. A bullet field is the requisition id only when the path
    ends with it, alone or followed by such a counter; the first bullet
    field is a location or a brand on many tenants. The counter is removed
    from the path only in that case: ``R-2026-12`` can be a requisition id
    of its own, and is kept whole when a bullet field says so.
    """
    if not isinstance(external_path, str) or not isinstance(bullet_fields, list):
        return None, external_path
    path = external_path.strip().rstrip("/")
    final_segment = path.rsplit("/", 1)[-1].casefold()
    whole: list[str] = []
    counted: list[str] = []
    exact_candidate: str | None = None
    for value in bullet_fields:
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned = value.strip()
        marker = cleaned.casefold()
        if not (
            len(cleaned) <= 64
            and any(character.isdigit() for character in cleaned)
            and re.fullmatch(r"[A-Za-z0-9._-]+", cleaned)
        ):
            continue
        if final_segment.endswith(f"_{marker}"):
            whole.append(cleaned)
        elif re.fullmatch(rf".*_{re.escape(marker)}-\d{{1,3}}", final_segment):
            counted.append(cleaned)
        elif final_segment == marker:
            exact_candidate = cleaned
    if whole:
        return min(whole, key=len), external_path
    if counted:
        return max(counted, key=len), _POSTING_COUNTER_RE.sub("", path)
    return exact_candidate, external_path


def _format_locations(primary: object, additional: object) -> str | None:
    """Combine primary + additional Workday location strings into a single
    pipe-separated value. Returns None when both are empty or non-string."""
    locs: list[str] = []
    if isinstance(primary, str) and primary.strip():
        locs.append(primary.strip())
    if isinstance(additional, list):
        for v in additional:
            if isinstance(v, str) and v.strip() and v.strip() not in locs:
                locs.append(v.strip())
    return " | ".join(locs) if locs else None


def _extract_description(job_posting_info: dict[str, Any]) -> str | None:
    """Return Workday's full posting body as plain text.

    The canonical detail field is ``jobDescription`` and is usually HTML.
    A few tenants expose closely named fallback fields, so try those before
    giving up.
    """
    for key in ("jobDescription", "externalJobDescription", "description"):
        value = job_posting_info.get(key)
        if isinstance(value, str) and value.strip():
            text = html_mod.unescape(value)
            text = _TAG_RE.sub(" ", text)
            text = html_mod.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            return text or None
    return None


def _pick_subdivision_facet(
    facets: list[dict[str, Any]],
    *,
    already_applied: set[str],
    size: int | None = None,
) -> tuple[str, list[tuple[str, int]]] | None:
    """Return ``(param, [(value_id, value_count), ...])`` for the best facet
    to subdivide further on, or ``None`` if nothing useful remains.

    Skips facets that are already in ``already_applied`` (reusing the same
    facet would just hit the cap again) and facets with fewer than two
    non-empty values. A facet whose counts add up to less than the search's
    job count leaves the jobs without a value out of every split, so it is
    used only when no other facet covers them all. The count is ``size``
    or the largest sum of a single-value facet, whichever is higher. Tries
    the priority list first, then falls back to the highest-cardinality
    remaining facet.
    """
    values = _facet_values(facets)
    by_param = {
        param: items
        for param, items in values.items()
        if param not in already_applied and len(items) >= 2
    }
    size = max(
        [size or 0]
        + [sum(count for _, count in values[p]) for p in _SINGLE_VALUE_FACETS if p in values]
    )
    if size:
        # Counts drift by a few jobs between two searches.
        complete = {
            param: items
            for param, items in by_param.items()
            if sum(count for _, count in items) >= size * 0.99
        }
        if by_param and not complete:
            logger.warning(
                "Workday: no facet covers all %d jobs of a capped search; "
                "the split leaves out the jobs without a value",
                size,
            )
        by_param = complete or by_param
    for preferred in _SUBDIVISION_FACETS:
        if preferred in by_param:
            return preferred, by_param[preferred]
    if by_param:
        param, values = max(by_param.items(), key=lambda kv: len(kv[1]))
        return param, values
    return None


def _facet_values(facets: object) -> dict[str, list[tuple[str, int]]]:
    """Every facet of a search response with its non-empty values.

    A facet whose values are facets of their own (``locationMainGroup``
    holds ``locationCountry`` and ``locations``) is replaced by them.
    """
    found: dict[str, list[tuple[str, int]]] = {}
    if not isinstance(facets, list):
        return found
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        values = [v for v in facet.get("values") or [] if isinstance(v, dict)]
        nested = [v for v in values if v.get("facetParameter")]
        if nested:
            for param, items in _facet_values(nested).items():
                found.setdefault(param, items)
            continue
        param = facet.get("facetParameter")
        if not isinstance(param, str) or not param or param in _IGNORED_FACETS:
            continue
        items: list[tuple[str, int]] = []
        for value in values:
            value_id = value.get("id")
            try:
                count = int(value.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if isinstance(value_id, str) and value_id and count > 0:
                items.append((value_id, count))
        if items:
            found.setdefault(param, items)
    return found
