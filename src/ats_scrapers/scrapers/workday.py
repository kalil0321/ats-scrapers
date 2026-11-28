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

The ``facets`` field in every response carries each value's true ``count``,
so the subdivision is planned without extra probes. Three traps: a facet
the search filters on counts as if that filter were not applied, a facet
lists at most 55 values, and jobs with no value for a facet are in none of
its filtered searches. When the split facet misses jobs, one more search
filtered on all its values shows, by the counts of another facet, where
the missing jobs are, and those values are read too.

Workday's Cloudflare front answers many plain Python clients with an HTTP 429
bot challenge, depending on the TLS fingerprint and the address. Requests go
through ``httpcloak`` (browser TLS) when it is installed, through httpx
otherwise, and through httpx with the caller's ``transport`` when one is given.
"""

from __future__ import annotations

import asyncio
import email.utils
import itertools
import json
import logging
import math
import random
import re
import threading
import time
import urllib.request
import weakref
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import httpx

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import Engine, HostPacer, _cloak_available, borrowed_transport
from ats_scrapers.models import ATSType, Job, JobLocation, WorkplaceType
from ats_scrapers.scrapers._job_places import distinct_places, job_location, workplace_type
from ats_scrapers.scrapers._posting_detail import body_text
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable
    from typing import Any, Self, TypeVar

    T = TypeVar("T")

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
# A 403 or 429 without Retry-After asks to slow down: wait about 5 s, then 10 s.
RATE_LIMIT_BACKOFF = 5.0
# A longer Retry-After ends the board: a sooner retry would be refused again.
MAX_RETRY_DELAY = 30.0
# Another 401 means a CSRF-protected tenant, which needs a handshake this scraper lacks.
_RETRYABLE_STATUSES = frozenset({403, 429, 502, 503, 504})
LISTING_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
# httpcloak replaces the User-Agent with its browser's own.
DETAIL_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
# ``get_description`` keeps its connections to the site open between jobs,
# enough of them for a caller that reads several jobs at once.
DETAIL_KEEPALIVE_CONNECTIONS = 8
DETAIL_KEEPALIVE_SECONDS = 60.0

# When a Workday job spans multiple offices, the search endpoint returns a
# rollup string in ``locationsText`` like "2 Locations" / "5 Locations"
# instead of the actual list — the underlying ``locations`` array is not
# included in the search payload. ``_enrich_details`` detects these and
# overwrites the placeholder with the real city list from
# ``jobPostingInfo.location`` + ``additionalLocations``.
_LOCATION_ROLLUP_RE = re.compile(r"^\s*\d+\s+Locations?\s*$", re.IGNORECASE)

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
# Some tenants use ``workerSubType`` for skills, several per posting.
_SINGLE_VALUE_FACETS = ("jobFamilyGroup", "timeType")
# A facet lists at most this many values: one listing this many may leave
# some out.
FACET_VALUE_CAP = 55
# A facet whose counts add up to within this share of a search's size covers
# it: counts drift by a few jobs between two searches.
_COVERAGE_TOLERANCE = 0.01
# Workday answers HTTP 500 to a search filtered on 500 values of one facet.
_MAX_FILTER_VALUES = 100
# Facets the jobs a split missed are looked for through, one after another:
# the n-th takes 2**n - 1 searches to count the jobs the others reached.
_REMAINDER_FACETS = 3


@ScraperRegistry.register(ATSType.WORKDAY)
class WorkdayScraper(BaseScraper):
    """Workday scraper — `company_slug` must be the full careers URL."""

    ats = ATSType.WORKDAY
    #: ``"cloak"`` falls back to ``"httpx"`` when httpcloak is not installed.
    fetch_engine: ClassVar[Engine] = "cloak"

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_fetch_seconds: float | None = None,
        seconds_per_job: float = 0.0,
        company_name: str | None = None,
        include_descriptions: bool = True,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        pacer: HostPacer | None = None,
    ) -> None:
        """``transport`` sends every request in place of httpcloak, a direct
        connection or ``proxy``. :meth:`get_description` needs it to be an
        ``httpx.BaseTransport`` as well. The scraper never closes it.

        ``pacer`` spaces the requests to each host, shared with any other
        scraper given the same pacer.

        ``seconds_per_job`` extends ``max_fetch_seconds`` by that much for
        each job the board announces, so a large board read through a
        shared pacer gets the time its pages take."""
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
            transport=transport,
        )
        self.max_fetch_seconds = max_fetch_seconds
        self.seconds_per_job = max(0.0, seconds_per_job)
        self.pacer = pacer
        self.company_name = (
            company_name.strip()
            if company_name and company_name.strip()
            else None
        )
        self._deadline: float | None = None
        self._detail_http: httpx.Client | _CloakClient | None = None
        self._detail_finalizer: weakref.finalize | None = None
        self._detail_lock = threading.Lock()

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
        ``company_name``, ``include_descriptions``, ``proxy``, ``transport``,
        ``pacer``).
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

    def _extend_deadline(self, jobs: int) -> None:
        if self._deadline is not None and self.seconds_per_job and jobs > 0:
            self._deadline += jobs * self.seconds_per_job

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise self._deadline_error()

    def _deadline_error(self, waiting_out: str | None = None) -> ScraperError:
        extra = f" plus {self.seconds_per_job:g} s per job" if self.seconds_per_job else ""
        reason = f" (waiting out {waiting_out})" if waiting_out else ""
        return ScraperError(
            f"Workday tenant exceeded max_fetch_seconds="
            f"{self.max_fetch_seconds:g}{extra}: {self.company_slug}{reason}"
        )

    def _remaining(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

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
        external_path = (job.raw or {}).get("externalPath") or _external_path(job.url)
        if not external_path:
            return None
        url = f"{detail_prefix}{external_path}"
        try:
            client = self._detail_client(url)
            if self.pacer is not None:
                self.pacer.wait_sync(url)
            response = client.get(url, headers=DETAIL_HEADERS)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        self._apply_detail(job, payload)
        return job.description

    def _apply_detail(self, job: Job, payload: object) -> None:
        if not isinstance(payload, dict) or not isinstance(info := payload.get("jobPostingInfo"), dict):
            return
        organization = payload.get("hiringOrganization")
        if not self.company_name and isinstance(organization, dict):
            name = organization.get("name")
            if isinstance(name, str) and name.strip():
                job.company = name.strip()
        if not job.description and (text := _extract_description(info)):
            job.description = text[:25_000]
        if places := _detail_places(info):
            job.locations = places
        additional = info.get("additionalLocations")
        label = _format_locations(info.get("location"), additional)
        if label and (not job.location or _LOCATION_ROLLUP_RE.match(job.location) or additional):
            job.location = label
        if mode := _attendance(info.get("remoteType")):
            job.workplace_type = mode
        dates = {key: info[key] for key in ("startDate", "endDate") if info.get(key)}
        if dates:
            job.raw = {**(job.raw or {}), **dates}
        if posted := _posting_day(dates.get("startDate")):
            job.posted_at = posted
        if deadline := _posting_day(dates.get("endDate")):
            job.application_deadline = deadline

    def _detail_client(self, url: str) -> httpx.Client | _CloakClient:
        """The client ``get_description`` reads with, opened on first use.

        Callers ask for one job at a time. Every job of a site lives on the
        same host, so the connection is kept between calls instead of being
        opened again for each job. :meth:`close` releases it, and so does
        discarding the scraper.
        """
        with self._detail_lock:
            if self._detail_http is None:
                client: httpx.Client | _CloakClient
                if self.transport is not None:
                    if not isinstance(self.transport, httpx.BaseTransport):
                        raise TypeError(
                            "get_description sends synchronous requests: the "
                            "transport must also be an httpx.BaseTransport"
                        )
                    client = httpx.Client(
                        timeout=self.timeout,
                        follow_redirects=True,
                        transport=borrowed_transport(self.transport),
                    )
                elif self._uses_cloak():
                    client = _CloakClient(timeout=self.timeout, proxy=self._cloak_proxy(url))
                else:
                    client = httpx.Client(
                        timeout=self.timeout,
                        follow_redirects=True,
                        proxy=self.proxy,
                        limits=httpx.Limits(
                            max_keepalive_connections=DETAIL_KEEPALIVE_CONNECTIONS,
                            keepalive_expiry=DETAIL_KEEPALIVE_SECONDS,
                        ),
                    )
                self._detail_finalizer = weakref.finalize(self, client.close)
                self._detail_http = client
            return self._detail_http

    def _uses_cloak(self) -> bool:
        if self.transport is not None or self.fetch_engine != "cloak":
            return False
        if _cloak_available():
            return True
        _warn_without_cloak()
        return False

    def _cloak_proxy(self, url: str) -> str | None:
        """The proxy httpx would use for ``url``: the given one, else the
        environment's."""
        if self.proxy:
            return self.proxy
        proxies = urllib.request.getproxies_environment()
        host = urlsplit(url).hostname or ""
        if urllib.request.proxy_bypass_environment(host, proxies):  # type: ignore[attr-defined]
            return None
        return proxies.get("https") or proxies.get("all")

    def _listing_client(self, url: str) -> httpx.AsyncClient | _AsyncCloakClient:
        if self.transport is not None:
            return httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                transport=borrowed_transport(self.transport),
            )
        if self._uses_cloak():
            return _AsyncCloakClient(
                _CloakClient(timeout=self.timeout, proxy=self._cloak_proxy(url))
            )
        return httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, proxy=self.proxy)

    def close(self) -> None:
        """Release the connection ``get_description`` keeps open.

        Safe to call more than once. A later ``get_description`` opens a
        new connection.
        """
        with self._detail_lock:
            finalizer, self._detail_finalizer = self._detail_finalizer, None
            self._detail_http = None
        if finalizer is not None:
            finalizer()

    async def _fetch_all(
        self,
        api: str,
        base: str,
        display_company: str,
        detail_prefix: str,
    ) -> list[Job]:
        async with self._listing_client(api) as client:
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
        client: httpx.AsyncClient | _AsyncCloakClient,
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
            j for j in jobs
            if (j.raw or {}).get("externalPath") or _external_path(j.url)
        ]
        if not targets:
            return

        refused: list[int] = []

        async def resolve(job: Job) -> None:
            external_path = (job.raw or {}).get("externalPath") or _external_path(job.url)
            if not external_path:
                return
            url = f"{detail_prefix}{external_path}"
            async with sem:
                if refused:
                    return
                if self.pacer is not None:
                    try:
                        await self.pacer.wait(url, timeout=self._remaining())
                    except TimeoutError:
                        return
                    # Another description may have been refused meanwhile.
                    if refused:
                        return
                try:
                    response = await client.get(url, headers=DETAIL_HEADERS)
                except httpx.HTTPError:
                    return
            if response.status_code == 429 or _challenged(response):
                refused.append(response.status_code)
                return
            if response.status_code != 200:
                return
            try:
                payload = response.json()
            except ValueError:
                return
            self._apply_detail(job, payload)

        await _run_all(resolve(job) for job in targets)
        if refused:
            logger.warning(
                "Workday (%s): a description request got HTTP %d, so the "
                "descriptions still missing were not requested",
                self.company_slug, refused[0],
            )

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient | _AsyncCloakClient,
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
        if depth == 0:
            self._extend_deadline(_job_count(total, first.get("facets") or []))
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
        facets = first.get("facets") or []
        applied = set(applied_facets)
        size = _job_count(max(size or 0, total), facets, applied)
        facet = (
            _pick_subdivision_facet(facets, already_applied=applied, size=size)
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

        await _run_all(child(v_id, count) for v_id, count in values)
        # The size is a floor: a facet that seems to cover it may still miss jobs.
        await self._exhaust_remainder(
            client, api, sem,
            applied_facets=applied_facets, covered=[(param, [v_id for v_id, _ in values])],
            facets=facets, absorb=absorb, depth=depth, size=size,
        )

    async def _exhaust_remainder(
        self,
        client: httpx.AsyncClient | _AsyncCloakClient,
        api: str,
        sem: asyncio.Semaphore,
        *,
        applied_facets: dict[str, list[str]],
        covered: list[tuple[str, list[str]]],
        facets: list[dict[str, Any]],
        absorb,
        depth: int,
        size: int,
    ) -> bool:
        """Read the jobs of a capped search that none of the ``covered``
        facet values holds. False when some seem missed and no other facet
        tells where they are.

        No search excludes a facet, so searches filtered on the covered
        values count the jobs already read, their union by inclusion and
        exclusion over the covered facets. For another facet, each value's
        count in the capped search (``facets``) less that union is how many
        missed jobs it holds. The facet whose values hold the most is used,
        then the jobs it misses too are looked for through the next one.
        A value is read whole when that costs at most a page for each missed
        job; a larger one is narrowed down the same way.
        """
        if any(len(ids) > _MAX_FILTER_VALUES for _, ids in covered):
            return False
        reached: list[tuple[int, dict[str, Any]]] = []
        for n in range(1, len(covered) + 1):
            for subset in itertools.combinations(covered, n):
                self._check_deadline()
                payload = await self._request(
                    client, api, sem,
                    applied_facets={**applied_facets, **dict(subset)}, offset=0,
                )
                if payload is None:
                    return False
                absorb(payload.get("jobPostings") or [])
                reached.append((1 if n % 2 else -1, payload))
        skip = {*applied_facets, *(param for param, _ in covered)}
        plan = _remainder_plan(
            facets, [(sign, payload.get("facets") or []) for sign, payload in reached], skip=skip,
        )
        if plan is None:
            if len(covered) > 1:
                return True
            missed = self._unreached(applied_facets, covered[0], facets, reached[0][1], size)
            return missed is not None and missed <= 0
        other, targets = plan

        async def recover(value_id: str, count: int, missed: int) -> None:
            self._check_deadline()
            filters = {**applied_facets, other: [value_id]}
            if count <= PAGE_LIMIT * max(missed, 5) or depth + 1 >= MAX_SUBDIVISION_DEPTH:
                await self._exhaust_query(
                    client, api, sem,
                    applied_facets=filters, absorb=absorb, depth=depth + 1, size=count,
                )
                return
            narrowed = await self._request(
                client, api, sem, applied_facets=filters, offset=0,
            )
            if narrowed is None:
                return
            absorb(narrowed.get("jobPostings") or [])
            located = await self._exhaust_remainder(
                client, api, sem,
                applied_facets=filters, covered=covered,
                facets=narrowed.get("facets") or [], absorb=absorb,
                depth=depth + 1, size=count,
            )
            if not located:
                # Nothing tells the missed jobs apart: read what the search shows.
                await self._fan_out_pages(
                    client, api, sem, applied_facets=filters,
                    total=min(int(narrowed.get("total", 0)), QUERY_TOTAL_CAP), absorb=absorb,
                )

        await _run_all(recover(*target) for target in targets)
        if len(covered) < _REMAINDER_FACETS:
            listed = [v_id for v_id, _ in _facet_values(facets).get(other, ())]
            await self._exhaust_remainder(
                client, api, sem,
                applied_facets=applied_facets, covered=[*covered, (other, listed)],
                facets=facets, absorb=absorb, depth=depth, size=size,
            )
        return True

    def _unreached(
        self,
        applied_facets: dict[str, list[str]],
        covered: tuple[str, list[str]],
        facets: list[dict[str, Any]],
        reached: dict[str, Any],
        size: int,
    ) -> int | None:
        """How many jobs of a capped search the split facet misses and no
        other facet reaches, logged; None when that cannot be counted."""
        param, value_ids = covered
        filtered = sorted(applied_facets) or "nothing"
        total = int(reached.get("total", 0))
        counts = dict(_facet_values(facets).get(param, ()))
        listed = sum(counts.get(v_id, 0) for v_id in value_ids)
        if total < QUERY_TOTAL_CAP:
            reached_jobs = total
        elif listed <= size * (1 + _COVERAGE_TOLERANCE):
            # One value per posting: the split values' counts add up to the jobs reached.
            reached_jobs = max(
                listed,
                _job_count(total, reached.get("facets") or [], {*applied_facets, param}),
            )
        else:
            logger.warning(
                "Workday (%s): a search filtered on %s holds %d jobs; %s gives some "
                "several values, so the jobs with none cannot be counted",
                self.company_slug, filtered, size, param,
            )
            return None
        missed = size - reached_jobs
        if missed > PAGE_LIMIT:
            logger.warning(
                "Workday (%s): about %d of %d jobs of a search filtered on %s "
                "have no %s, and no other facet reaches them",
                self.company_slug, missed, size, filtered, param,
            )
        return missed

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient | _AsyncCloakClient,
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

        results = await _run_all(fetch_one(o) for o in offsets)
        for batch in results:
            absorb(batch)

    async def _request(
        self,
        client: httpx.AsyncClient | _AsyncCloakClient,
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
        last_error = "no response"
        delay = 0.0
        for attempt in range(MAX_RETRIES):
            if attempt:
                await self._wait_to_retry(delay, last_error)
            self._check_deadline()
            async with sem:
                if self.pacer is not None:
                    try:
                        await self.pacer.wait(api, timeout=self._remaining())
                    except TimeoutError:
                        raise self._deadline_error() from None
                try:
                    response = await client.post(api, json=body, headers=LISTING_HEADERS)
                except httpx.HTTPError as exc:
                    last_error = (
                        f"{type(exc).__name__}: {exc}" if str(exc)
                        else type(exc).__name__
                    )
                    delay = _jittered(RETRY_BACKOFF ** attempt)
                    continue
            status = response.status_code
            if _challenged(response):
                raise ScraperError(
                    f"Workday gave up at offset={offset}: HTTP {status}, a bot challenge"
                )
            if status == 404:
                raise CompanyNotFoundError(
                    f"Workday site not found: {self.company_slug}"
                )
            if offset == 0 and not applied_facets:
                gone = _tenant_gone(response)
                if gone:
                    raise CompanyNotFoundError(
                        f"Workday tenant {self.company_slug} {gone}"
                    )
            if status == 200:
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
                    delay = _jittered(RETRY_BACKOFF ** attempt)
                    continue
                if not isinstance(payload, dict):
                    last_error = (
                        "Workday returned an unexpected JSON payload for "
                        f"{self.company_slug} (offset={offset})"
                    )
                    delay = _jittered(RETRY_BACKOFF ** attempt)
                    continue
                return payload
            if status in _RETRYABLE_STATUSES:
                last_error = f"HTTP {status}"
                retry_after = _retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > MAX_RETRY_DELAY:
                    raise ScraperError(
                        f"Workday gave up at offset={offset}: {last_error}, "
                        f"Retry-After {retry_after:.0f} s"
                    )
                if retry_after is None:
                    base = (
                        RATE_LIMIT_BACKOFF * 2 ** attempt if status in (403, 429)
                        else RETRY_BACKOFF ** attempt
                    )
                    retry_after = _jittered(min(MAX_RETRY_DELAY, base))
                delay = retry_after
                continue
            raise ScraperError(
                f"Workday returned {status} for {self.company_slug} "
                f"(offset={offset})"
            )
        raise ScraperError(
            f"Workday gave up after {MAX_RETRIES} retries at offset={offset}: {last_error}"
        )

    async def _wait_to_retry(self, delay: float, last_error: str) -> None:
        if self._deadline is not None and time.monotonic() + delay >= self._deadline:
            raise self._deadline_error(last_error)
        await asyncio.sleep(delay)

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
            locations=_listing_places(item.get("locationsText")),
            workplace_type=_attendance(remote_type),
            is_remote=is_remote,
            employment_type=employment_type,
            commitment=commitment,
            department=department,
            requisition_id=requisition_id,
            posted_at=_parse_workday_date(item.get("postedOn")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _challenged(response: Any) -> bool:
    """Whether Cloudflare answered with its bot check. A client that cannot run
    the check gets it again on every retry."""
    value = response.headers.get("cf-mitigated")
    return isinstance(value, str) and value.strip().lower() == "challenge"


def _retry_after(value: object) -> float | None:
    """Seconds to wait from a Retry-After header: a number or an HTTP date."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _jittered(delay: float) -> float:
    """Between half and all of ``delay``, so a site's requests do not retry
    in step."""
    return delay * random.uniform(0.5, 1.0)


async def _run_all(aws: Iterable[Awaitable[T]]) -> list[T]:
    """Await ``aws`` together. When one fails, the others are cancelled and
    awaited before the error is raised, so none outlives the client."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_warned_without_cloak = False


def _warn_without_cloak() -> None:
    global _warned_without_cloak
    if not _warned_without_cloak:
        _warned_without_cloak = True
        logger.warning(
            "Workday: httpcloak is not installed, so requests go through httpx, "
            "which Workday often answers with HTTP 429 bot challenges. "
            "Install ats-scrapers[scrapers]."
        )


class _CloakResponse:
    """An httpcloak answer with the parts of ``httpx.Response`` the scraper reads."""

    def __init__(self, raw: Any) -> None:
        self.status_code = int(raw.status_code)
        self.headers = httpx.Headers(_header_items(getattr(raw, "headers", None)))
        self.text: str = raw.text or ""

    def json(self) -> Any:
        return json.loads(self.text)


def _header_items(headers: object) -> list[tuple[str, str]]:
    """httpcloak gives each header as a string or as a list of strings."""
    if not isinstance(headers, dict):
        return []
    return [
        (str(name), str(one))
        for name, value in headers.items()
        for one in (value if isinstance(value, list) else [value])
    ]


class _CloakClient:
    """One httpcloak session (kept connections, browser TLS) for one site.

    Thread-safe. A request can outlive the task that started it in a worker
    thread, so the session is freed when its last request ends.
    """

    def __init__(self, *, timeout: float, proxy: str | None) -> None:
        import httpcloak  # type: ignore[import-untyped]

        try:
            self._session = httpcloak.Session(timeout=max(1, math.ceil(timeout)), proxy=proxy)
        except Exception as exc:
            raise ScraperError(f"Workday: httpcloak could not start a session: {exc}") from exc
        self._lock = threading.Lock()
        self._running = 0
        self._closing = False

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> _CloakResponse:
        return self._send("GET", url, headers=headers)

    def post(
        self, url: str, *, json: Any = None, headers: dict[str, str] | None = None
    ) -> _CloakResponse:
        return self._send("POST", url, json=json, headers=headers)

    def _send(
        self, method: str, url: str, *, json: Any = None, headers: dict[str, str] | None
    ) -> _CloakResponse:
        sent = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        with self._lock:
            if self._closing:
                raise RuntimeError("Cannot send a request, as the session has been closed.")
            self._running += 1
        try:
            if method == "POST":
                raw = self._session.post(url, json=json, headers=sent)
            else:
                raw = self._session.get(url, headers=sent)
        except Exception as exc:  # httpcloak reports every network failure this way
            raise httpx.TransportError(str(exc) or type(exc).__name__) from exc
        finally:
            with self._lock:
                self._running -= 1
                free = self._closing and not self._running
            if free:
                self._session.close()
        return _CloakResponse(raw)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            free = not self._running
        if free:
            self._session.close()


class _AsyncCloakClient:
    """:class:`_CloakClient` for the event loop, one worker thread per request."""

    def __init__(self, client: _CloakClient) -> None:
        self._client = client

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._client.close()

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _CloakResponse:
        return await asyncio.to_thread(self._client.get, url, headers=headers)

    async def post(
        self, url: str, *, json: Any = None, headers: dict[str, str] | None = None
    ) -> _CloakResponse:
        return await asyncio.to_thread(self._client.post, url, json=json, headers=headers)


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
    ``HTTP_422``; Workday answers a malformed search with 400 instead. A
    closed site answers 403 ``S22`` "permission denied" from Workday itself,
    not Cloudflare, and its page shows "Workday is currently unavailable". A
    closed tenant answers 401 "Unable to verify credentials for system
    account", and its page redirects to Workday's outage page.
    """
    if response.status_code not in (401, 403, 410, 422):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("errorCode")
    if response.status_code == 410 and code == "ERR_TENANT_MIGRATED":
        return "moved to another Workday data center"
    if response.status_code == 422 and code == "HTTP_422":
        return "is not served from this host: it moved or left Workday"
    message = payload.get("message")
    if (
        response.status_code == 401
        and code == "HTTP_401"
        and isinstance(message, str)
        and message.strip().casefold().startswith(
            "unable to verify credentials for system account"
        )
    ):
        return "is closed: Workday cannot verify its public account (401)"
    if (
        response.status_code == 403
        and code == "S22"
        and isinstance(message, str)
        and message.strip().casefold() == "permission denied"
    ):
        return "closed this site: Workday refuses its search (403 S22, permission denied)"
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


def _attendance(value: object) -> WorkplaceType | None:
    if isinstance(value, str) and value.strip().lower() in _REMOTE_TYPE_PATTERNS:
        return "remote" if _REMOTE_TYPE_PATTERNS[value.strip().lower()] else "onsite"
    return workplace_type(value)


def _listing_places(label: object) -> list[JobLocation] | None:
    if not isinstance(label, str) or _LOCATION_ROLLUP_RE.match(label):
        return None
    return distinct_places([job_location(label)])


def _detail_places(info: dict[str, Any]) -> list[JobLocation] | None:
    entry = info.get("jobRequisitionLocation")
    entry = entry if isinstance(entry, dict) else {}
    country = entry.get("country")
    code = country.get("alpha2Code") if isinstance(country, dict) else None
    primary = job_location(info.get("location") or entry.get("descriptor"), country_code=code, primary=True)
    places = [primary]
    additional = info.get("additionalLocations")
    if isinstance(additional, list):
        for value in additional:
            label = value.get("descriptor") if isinstance(value, dict) else value
            place = job_location(label, primary=False)
            if place and (primary is None or place.label != primary.label):
                places.append(place)
    return distinct_places(places)


def _posting_day(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


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
        if text := body_text(value):
            return text
    return None


def _job_count(
    total: int,
    facets: list[dict[str, Any]],
    applied: Iterable[str] = (),
) -> int:
    """A search's job count: its total, or more when the total is capped and
    the counts of a single-value facet add up to more. A facet the search
    filters on counts the jobs of the search without that filter, so it is
    left out."""
    values = _facet_values(facets)
    skip = set(applied)
    return max(
        [total]
        + [
            _facet_sum(values[p])
            for p in _SINGLE_VALUE_FACETS
            if p in values and p not in skip
        ]
    )


def _facet_sum(items: list[tuple[str, int]]) -> int:
    return sum(count for _, count in items)


def _covers(items: list[tuple[str, int]], size: int) -> bool:
    """Whether a facet's values hold every job of a search of ``size`` jobs.

    Counts that add up to more than the size come from postings with several
    values; a facet listing :data:`FACET_VALUE_CAP` of them may then have
    left out the values of the jobs it misses.
    """
    total = _facet_sum(items)
    if total < size * (1 - _COVERAGE_TOLERANCE):
        return False
    return len(items) < FACET_VALUE_CAP or total <= size * (1 + _COVERAGE_TOLERANCE)


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
    non-empty values. The search's job count is ``size`` or the largest sum
    of a single-value facet, whichever is higher. A facet that covers it is
    preferred, one value per posting first, as the priority list orders
    them, then the one with the most values. When none covers it, the facet
    that reaches the most jobs is used.
    """
    values = _facet_values(facets)
    by_param = {
        param: items
        for param, items in values.items()
        if param not in already_applied and len(items) >= 2
    }
    if not by_param:
        return None
    size = _job_count(size or 0, facets, already_applied)
    rank = {param: i for i, param in enumerate(_SUBDIVISION_FACETS)}
    complete = {param: items for param, items in by_param.items() if _covers(items, size)}
    if complete:
        exact = {
            param: items
            for param, items in complete.items()
            if _facet_sum(items) <= size * (1 + _COVERAGE_TOLERANCE)
        }
        pool = exact or complete
        param = min(pool, key=lambda p: (rank.get(p, len(rank)), -len(pool[p])))
        return param, pool[param]

    def reach(param: str) -> tuple[bool, int, int, int]:
        items = by_param[param]
        # Several values per posting: the sum says little of the reach.
        stacked = len(items) >= FACET_VALUE_CAP and _facet_sum(items) > size
        return (
            not stacked,
            min(_facet_sum(items), size),
            -rank.get(param, len(rank)),
            len(items),
        )

    param = max(by_param, key=reach)
    return param, by_param[param]


def _remainder_plan(
    facets: list[dict[str, Any]],
    reached: list[tuple[int, list[dict[str, Any]]]],
    *,
    skip: set[str],
) -> tuple[str, list[tuple[str, int, int]]] | None:
    """Where the jobs a split missed are: ``(param, [(value_id, count, missed)])``.

    ``facets`` are the capped search's. ``reached`` holds the facets of the
    searches filtered on the values read, each with the sign it adds to the
    count of jobs read. A value's count in the capped search less that sum is
    how many missed jobs it holds. A listing of :data:`FACET_VALUE_CAP` values
    keeps the largest, so a value it leaves out counts at most its smallest:
    the missed jobs are then counted at least. The facet whose values hold
    the most is picked.
    """
    node = _facet_values(facets)
    searches = [(sign, _facet_values(found)) for sign, found in reached]
    best: tuple[str, list[tuple[str, int, int]]] | None = None
    best_key: tuple[int, int] | None = None
    for param, items in node.items():
        if param in skip:
            continue
        lists = [(sign, dict(values.get(param, ()))) for sign, values in searches]
        targets: list[tuple[str, int, int]] = []
        for value_id, count in items:
            missed = count
            for sign, counts in lists:
                if value_id in counts:
                    missed -= sign * counts[value_id]
                elif sign > 0 and len(counts) >= FACET_VALUE_CAP:
                    missed -= min(counts.values())
            if missed > 0:
                targets.append((value_id, count, missed))
        if not targets:
            continue
        key = (sum(missed for *_, missed in targets), -len(targets))
        if best_key is None or key > best_key:
            best, best_key = (param, targets), key
    return best


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
