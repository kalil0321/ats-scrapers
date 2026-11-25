"""Eightfold AI careers scraper — generic, multi-tenant.

Eightfold AI ("PCSX") powers the careers sites of many large enterprises:
Microsoft, Nvidia, Cisco, AT&T, Booking, Dolby, Activision, Bayer, etc. They
all share the same search endpoint:

    GET {tenant_url}/api/pcsx/search
        ?domain={company_domain}&query=&start=N&sort_by=timestamp

`tenant_url` is one of two shapes:
- ``https://{slug}.eightfold.ai``              — Eightfold-hosted (most tenants)
- ``https://apply.careers.{company}.com``      — custom domain (Microsoft, ...)

The response shape is identical across tenants. Each page returns 10
positions (server-side cap; ``num_results``, ``size``, etc. are all ignored)
plus ``data.count`` = the true total. We use that to fan out the remaining
pages concurrently.

Some tenants sit behind Cloudflare and 403 plain ``httpx`` requests
(observed: Bayer, AT&T, Activision, Verizon). For those we fall back to
``httpcloak`` (browser TLS fingerprinting). With ``client_kind="auto"``
(the default) we probe with httpx and switch to httpcloak only on 403.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import httpx

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job, JobLocation
from ats_scrapers.scrapers._job_places import distinct_places, job_location, workplace_type
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

PAGE_SIZE = 10  # Eightfold's server-fixed page size.
MAX_CONCURRENCY_HTTPX = 12  # PCSX comfortably handles this; raise carefully.
MAX_CONCURRENCY_HTTPCLOAK = 4  # browser-fingerprint clients are heavier per-request
DETAIL_CONCURRENCY = 8  # per-tenant detail-page enrichment cap
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5  # seconds; exponential for 429, linear for 5xx
# A detail 429 that asks for a longer wait than this ends the detail calls
# for the run. The wait is slept once per job while holding a slot, so a
# tenant answering ``Retry-After: 3600`` would otherwise stall its run for
# hours for descriptions it is refusing to serve anyway.
DETAIL_MAX_RETRY_WAIT = 60.0
SLOW_REQUEST_THRESHOLD = 5.0  # log a warning for any single request slower than this
# Eightfold's edge answers 405 to every tenant, on every host, once one address
# has sent too many requests, and serves it again a few minutes after the
# requests stop. A listing page that meets it waits these delays in turn.
BLOCK_COOLOFF_DELAYS: tuple[float, ...] = (30.0, 90.0, 180.0)
# A board answers 429 to an address that sends it a few hundred search requests
# in a burst (about 300 at MAX_CONCURRENCY_HTTPX), whatever the offset, while
# 3 requests per second pass for 600 pages. Past the first page, a 429 pauses
# every listing request of the board for these delays in turn, then the board
# is paced. Each pause that ends with no page served moves to the next delay.
RATE_LIMIT_COOLOFF_DELAYS: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)
# Seconds between listing requests once a board answered 429. A 429 that follows
# a page served at this pace doubles it; one met before any page only lengthens
# the pause.
RATE_LIMITED_INTERVAL = 0.5
MAX_RATE_LIMITED_INTERVAL = 4.0
# Eightfold's 404 page for a host whose company group no longer exists. A live
# tenant asked with a wrong ``domain`` answers a plain "Page Not Found".
_GROUP_NOT_FOUND = "Group ID not found"

_TAG_RE_EF = re.compile(r"<[^>]+>")

ClientKind = Literal["auto", "httpx", "httpcloak"]


@ScraperRegistry.register(ATSType.EIGHTFOLD)
class EightfoldScraper(BaseScraper):
    """Generic Eightfold scraper — works on any PCSX-powered careers site.

    `company_slug` is the Eightfold subdomain by default
    (e.g. ``"nvidia"`` → ``https://nvidia.eightfold.ai``). Override
    `base_url` for tenants on custom domains and `domain` for the company
    domain that the API expects.

    `client_kind`:
    - ``"auto"`` (default): try httpx first, fall back to httpcloak on 403.
    - ``"httpx"``: pin to httpx (raise on 403, no fallback).
    - ``"httpcloak"``: skip the probe, go straight to httpcloak.
    """

    ats = ATSType.EIGHTFOLD

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        base_url: str | None = None,
        domain: str | None = None,
        company_name: str | None = None,
        job_url_host: str | None = None,
        client_kind: ClientKind = "auto",
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        company_slug = require_host_label(company_slug, provider="EightfoldScraper")
        self.company_slug = company_slug
        if base_url is not None:
            base_url = require_http_url(base_url, provider="EightfoldScraper")
        self.base_url = (base_url or f"https://{company_slug}.eightfold.ai").rstrip("/")
        self.domain = domain or f"{company_slug}.com"
        self.company_name = company_name or company_slug.replace("-", " ").title()
        # Some tenants (notably Microsoft) serve the API on one host but
        # render job URLs on a different one. Default to the API host.
        self.job_url_host = (job_url_host or self.base_url).rstrip("/")
        self.client_kind: ClientKind = client_kind
        # Set when position_details answers 403/405 so the remaining per-job
        # detail calls are skipped instead of hammering a blocking tenant.
        self._detail_blocked: bool = False

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        # A standalone detail call is its own run; a block seen during an
        # earlier afetch must not silence it.
        self._detail_blocked = False
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
                proxy=self.proxy,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_position_details(client, sem, copy)
            return copy.description

        return self._run_sync(run())

    # --- async pipeline -------------------------------------------------

    async def afetch(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        self._detail_blocked = False

        if self.client_kind == "httpcloak":
            await asyncio.to_thread(self._fetch_via_httpcloak_sync, seen, all_jobs)
            return all_jobs

        # httpx or auto: try httpx first
        try:
            await self._fetch_via_httpx(seen, all_jobs)
        except _SmartApplyRequired:
            seen.clear()
            all_jobs.clear()
            await self._fetch_via_smartapply_httpx(seen, all_jobs)
        except _WAFBlocked as exc:
            if self.client_kind == "httpx":
                raise ScraperError(
                    f"Eightfold ({self.company_name}) blocked by WAF (403); "
                    f"set client_kind='httpcloak' to bypass"
                ) from exc
            # auto: switch to httpcloak
            seen.clear()
            all_jobs.clear()
            await asyncio.to_thread(self._fetch_via_httpcloak_sync, seen, all_jobs)
        return all_jobs

    # --- httpx path -----------------------------------------------------

    async def _fetch_via_httpx(self, seen: set[str], all_jobs: list[Job]) -> None:
        # Keepalive over MAX_CONCURRENCY connections amortizes the TLS
        # handshake — meaningful on tenants with many pages.
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENCY_HTTPX * 2,
                max_keepalive_connections=MAX_CONCURRENCY_HTTPX,
            ),
            proxy=self.proxy,
        ) as client:
            first = await self._fetch_page_httpx(client, start=0)
            self._collect(first.get("positions") or [], seen, all_jobs)
            count = int(first.get("count") or 0)
            if count <= PAGE_SIZE:
                return
            offsets = list(range(PAGE_SIZE, count, PAGE_SIZE))
            sem = asyncio.Semaphore(MAX_CONCURRENCY_HTTPX)
            pace = _ListingPace(self.company_name)

            async def task(offset: int) -> None:
                async with sem:
                    page = await self._fetch_page_httpx(client, start=offset, pace=pace)
                    self._collect(page.get("positions") or [], seen, all_jobs)

            # ``asyncio.gather`` leaves sibling tasks running when one raises
            # (e.g. a late ``_SmartApplyRequired`` 403 on a fanned-out page).
            # Cancel and drain them before propagating so nothing writes to
            # ``all_jobs`` after the caller clears it for the SmartApply retry.
            await _gather_cancel_on_error(*(task(o) for o in offsets))

            # Detail enrichment — pull jobDescription from the public
            # `position_details` XHR endpoint per job. PCSX handles the
            # higher request volume well; the search calls were ~10
            # pages × 12 concurrency, the detail pass is per-job but
            # still fits inside the same WAF budget.
            if self.include_descriptions and all_jobs:
                detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_position_details(client, detail_sem, j)
                    for j in all_jobs
                ))

    async def _fetch_via_smartapply_httpx(
        self, seen: set[str], all_jobs: list[Job]
    ) -> None:
        """Fetch tenants backed by Eightfold's public SmartApply endpoint."""
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENCY_HTTPX * 2,
                max_keepalive_connections=MAX_CONCURRENCY_HTTPX,
            ),
            proxy=self.proxy,
        ) as client:
            first = await self._fetch_page_smartapply_httpx(client, start=0)
            self._collect(first.get("positions") or [], seen, all_jobs)
            count = int(first.get("count") or 0)
            if count <= PAGE_SIZE:
                return
            offsets = list(range(PAGE_SIZE, count, PAGE_SIZE))
            sem = asyncio.Semaphore(MAX_CONCURRENCY_HTTPX)
            pace = _ListingPace(self.company_name)

            async def task(offset: int) -> None:
                async with sem:
                    page = await self._fetch_page_smartapply_httpx(
                        client, start=offset, pace=pace
                    )
                    self._collect(page.get("positions") or [], seen, all_jobs)

            await _gather_cancel_on_error(*(task(o) for o in offsets))

    async def _enrich_position_details(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        """Hydrate ``job`` from `/api/pcsx/position_details`.

        Best-effort — non-200 responses, JSON-shape changes, or transient
        WAF hits leave the listing-derived fields intact.
        """
        if self._detail_blocked:
            return
        # Use the position id (matches search response's ``id``, not
        # ``displayJobId``). The id is encoded in the position URL.
        position_id = _position_id_from_url(job.url)
        if not position_id:
            return
        response = await self._get_position_details(client, sem, position_id)
        if response is None:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        data = payload.get("data") or {}
        desc_html = data.get("jobDescription")
        if isinstance(desc_html, str) and desc_html.strip() and not job.description:
            job.description = _html_unescape_for_desc(desc_html, cap=25_000) or None

    async def _get_position_details(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        position_id: str,
    ) -> httpx.Response | None:
        """GET ``position_details`` with retry; ``None`` means give up.

        Retries network errors, 429 (honouring ``Retry-After``) and 5xx. A
        403/405 is a block: it is not retried and sets ``_detail_blocked`` so
        the rest of the detail swarm stops. So does a 429 whose
        ``Retry-After`` exceeds ``DETAIL_MAX_RETRY_WAIT``. The backoff sleeps happen while
        holding the semaphore slot on purpose, so a rate limit throttles the
        whole swarm and not just this one job.
        """
        async with sem:
            for attempt in range(1, MAX_RETRIES + 1):
                # A sibling task may have hit a block while we waited.
                if self._detail_blocked:
                    return None
                try:
                    response = await client.get(
                        f"{self.base_url}/api/pcsx/position_details",
                        params={
                            "position_id": position_id,
                            "domain": self.domain,
                            "hl": "en",
                        },
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError:
                    if attempt == MAX_RETRIES:
                        return None
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
                if response.status_code == 200:
                    return response
                if response.status_code in (403, 405):
                    self._note_detail_blocked(response.status_code)
                    return None
                if not _is_retryable_status(response.status_code):
                    return None
                delay = _detail_retry_delay(response, attempt)
                # Checked before the last-attempt return: a long cooldown
                # announced on the final attempt must stop the swarm too.
                if delay > DETAIL_MAX_RETRY_WAIT:
                    self._note_detail_blocked(response.status_code)
                    return None
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(delay)
        return None

    def _note_detail_blocked(self, status: int) -> None:
        if not self._detail_blocked:
            import logging
            logging.getLogger(__name__).warning(
                "Eightfold (%s) position_details blocked (%d); "
                "skipping remaining detail calls",
                self.company_name, status,
            )
        self._detail_blocked = True

    async def _fetch_page_httpx(
        self,
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None = None,
    ) -> dict[str, Any]:
        return await self._wait_out_rate_limit(
            self._fetch_page_httpx_once, client, start=start, pace=pace
        )

    async def _fetch_page_smartapply_httpx(
        self,
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None = None,
    ) -> dict[str, Any]:
        return await self._wait_out_rate_limit(
            self._fetch_page_smartapply_httpx_once, client, start=start, pace=pace
        )

    async def _wait_out_rate_limit(
        self,
        fetch_page: Callable[..., Awaitable[dict[str, Any]]],
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None,
    ) -> dict[str, Any]:
        """Without a pace (the first page), a 429 is retried a few times by
        ``fetch_page`` and then fails the board."""
        while True:
            try:
                page = await self._wait_out_block(
                    fetch_page, client, start=start, pace=pace
                )
            except _RateLimited as exc:
                if pace is None:
                    raise
                pace.back_off(
                    generation=exc.generation, retry_after=exc.retry_after, start=start
                )
                continue
            return page

    async def _wait_out_block(
        self,
        fetch_page: Callable[..., Awaitable[dict[str, Any]]],
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None = None,
    ) -> dict[str, Any]:
        for delay in BLOCK_COOLOFF_DELAYS:
            try:
                return await fetch_page(client, start=start, pace=pace)
            except _EdgeBlocked:
                import logging
                logging.getLogger(__name__).warning(
                    "Eightfold (%s) blocked (405) at start=%d; retrying in %.0fs",
                    self.company_name, start, delay,
                )
                await asyncio.sleep(delay)
        try:
            return await fetch_page(client, start=start, pace=pace)
        except _EdgeBlocked as exc:
            raise ScraperError(
                f"Eightfold ({self.company_name}) blocked (405) at start={start} "
                f"after waiting {sum(BLOCK_COOLOFF_DELAYS):.0f}s"
            ) from exc

    async def _fetch_page_httpx_once(
        self,
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None = None,
    ) -> dict[str, Any]:
        """One page with retry on 429/5xx (ported from the legacy Microsoft
        scraper, where ~1% of requests hit transient 502s on Eightfold).
        With a ``pace``, a 429 is left to the pace instead."""
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            generation = await pace.wait() if pace is not None else 0
            t0 = asyncio.get_event_loop().time()
            try:
                response = await client.get(
                    f"{self.base_url}/api/pcsx/search",
                    params={
                        "domain": self.domain,
                        "query": "",
                        "location": "",
                        "start": start,
                        "sort_by": "timestamp",
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json, text/plain, */*",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) fetch failed at "
                        f"start={start}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue

            elapsed = asyncio.get_event_loop().time() - t0
            if elapsed > SLOW_REQUEST_THRESHOLD:
                # Visibility into pathological tenants without spamming logs.
                import logging
                logging.getLogger(__name__).warning(
                    "Eightfold (%s) slow request: %.1fs at start=%d",
                    self.company_name, elapsed, start,
                )

            if response.status_code == 200:
                if pace is not None:
                    pace.note_served(generation)
                return response.json().get("data") or {}
            if response.status_code == 403:
                if _is_pcsx_unavailable(response):
                    raise _SmartApplyRequired(self.company_name)
                raise _WAFBlocked(self.company_name, start)
            if response.status_code == 429:  # rate-limited
                if pace is not None:
                    raise _RateLimited(generation, _retry_after_header(response))
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) rate-limited (429) at "
                        f"start={start} after {MAX_RETRIES} retries"
                    )
                # Honour Retry-After if present, else exponential
                await asyncio.sleep(_retry_after_seconds(response, attempt))
                continue
            if 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) returned "
                        f"{response.status_code} at start={start} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 405:
                raise _EdgeBlocked
            self._raise_if_group_gone(response)
            # Non-retryable status (404, etc.)
            raise ScraperError(
                f"Eightfold ({self.company_name}) returned {response.status_code} at start={start}"
            )

        # Defensive — loop should always raise or return.
        raise ScraperError(
            f"Eightfold ({self.company_name}) exhausted retries at start={start}: {last_exc}"
        )

    async def _fetch_page_smartapply_httpx_once(
        self,
        client: httpx.AsyncClient,
        *,
        start: int,
        pace: _ListingPace | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            generation = await pace.wait() if pace is not None else 0
            try:
                response = await client.get(
                    f"{self.base_url}/api/apply/v2/jobs",
                    params={
                        "domain": self.domain,
                        "query": "",
                        "location": "",
                        "start": start,
                        "sort_by": "timestamp",
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json, text/plain, */*",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) SmartApply fetch "
                        f"failed at start={start}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) SmartApply returned "
                        f"malformed JSON at start={start}: {exc}"
                    ) from exc
                if pace is not None:
                    pace.note_served(generation)
                return {
                    "positions": payload.get("positions") or [],
                    "count": payload.get("count") or 0,
                }
            if response.status_code == 429 and pace is not None:
                raise _RateLimited(generation, _retry_after_header(response))
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) SmartApply returned "
                        f"{response.status_code} at start={start} after "
                        f"{MAX_RETRIES} retries"
                    )
                await asyncio.sleep(_retry_after_seconds(response, attempt))
                continue
            if response.status_code == 405:
                raise _EdgeBlocked
            if response.status_code == 403 and await self._careers_page_missing(client):
                raise CompanyNotFoundError(
                    f"Eightfold ({self.company_name}) refuses both job "
                    f"listings and has no careers page at {self.base_url}/careers"
                )
            raise ScraperError(
                f"Eightfold ({self.company_name}) SmartApply returned "
                f"{response.status_code} at start={start}"
            )

        raise ScraperError(
            f"Eightfold ({self.company_name}) SmartApply exhausted retries at "
            f"start={start}: {last_exc}"
        )

    async def _careers_page_missing(self, client: httpx.AsyncClient) -> bool:
        """Whether the board's own careers page answers 404, as it does once
        the employer turned its Eightfold site off."""
        try:
            response = await client.get(
                f"{self.base_url}/careers", headers={"User-Agent": "Mozilla/5.0"}
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 404

    # --- httpcloak path (sync, but parallelized via to_thread) ----------

    def _fetch_via_httpcloak_sync(
        self, seen: set[str], all_jobs: list[Job]
    ) -> None:
        try:
            import httpcloak  # noqa: F401  — availability check
        except ImportError as exc:
            raise ScraperError(
                "httpcloak required for this tenant; install with "
                "`pip install httpcloak` or `pip install ats-scrapers[scrapers]`"
            ) from exc

        first = self._fetch_page_httpcloak(start=0)
        self._collect(first.get("positions") or [], seen, all_jobs)
        count = int(first.get("count") or 0)
        if count <= PAGE_SIZE:
            return
        # Sequential fan-out — httpcloak.get is sync and re-entering threads
        # from a `to_thread` task is messy; sequential keeps it simple and
        # the WAF-blocked path is by definition the slow path.
        for offset in range(PAGE_SIZE, count, PAGE_SIZE):
            page = self._fetch_page_httpcloak(start=offset)
            self._collect(page.get("positions") or [], seen, all_jobs)

    def _fetch_page_httpcloak(self, *, start: int) -> dict[str, Any]:
        import httpcloak  # local import: optional dep

        kwargs: dict[str, Any] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            response = httpcloak.get(
                f"{self.base_url}/api/pcsx/search",
                params={
                    "domain": self.domain,
                    "query": "",
                    "location": "",
                    "start": start,
                    "sort_by": "timestamp",
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=self.timeout,
                **kwargs,
            )
        except Exception as exc:  # httpcloak may raise misc subclasses
            raise ScraperError(
                f"Eightfold ({self.company_name}) httpcloak failed at start={start}: {exc}"
            ) from exc
        if response.status_code != 200:
            self._raise_if_group_gone(response)
            raise ScraperError(
                f"Eightfold ({self.company_name}) httpcloak returned {response.status_code} at start={start}"
            )
        return response.json().get("data") or {}

    # --- shared helpers -------------------------------------------------

    def _raise_if_group_gone(self, response: Any) -> None:
        """Takes an httpx or an httpcloak response."""
        if response.status_code == 404 and _GROUP_NOT_FOUND in response.text:
            raise CompanyNotFoundError(
                f"Eightfold ({self.company_name}) has no company group "
                f"{self.domain} any more (Group ID not found)"
            )

    def _collect(
        self,
        positions: list[dict[str, Any]],
        seen: set[str],
        all_jobs: list[Job],
    ) -> None:
        for item in positions:
            job = self._parse_job(item)
            if job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            all_jobs.append(job)

    def _parse_job(self, item: dict[str, Any]) -> Job:
        ats_id = str(
            item.get("displayJobId")
            or item.get("display_job_id")
            or item.get("id")
            or item.get("atsJobId")
            or item.get("ats_job_id")
            or ""
        )
        position = (
            item.get("canonicalPositionUrl")
            or item.get("canonical_position_url")
            or item.get("positionUrl")
            or item.get("position_url")
            or ""
        )
        if position.startswith("/"):
            url = f"{self.job_url_host}{position}"
        elif position:
            url = position
        else:
            url = f"{self.job_url_host}/careers/job/{ats_id}"

        # Eightfold typically wraps a Workday or other underlying ATS — its
        # ``atsJobId`` / ``displayJobId`` is the upstream requisition id and
        # matches the underlying ATS's employer requisition id. That's the
        # signal the cross-ATS dedup pass uses (Pass 3).
        requisition_id = (
            item.get("atsJobId")
            or item.get("ats_job_id")
            or item.get("displayJobId")
            or item.get("display_job_id")
        )

        raw: dict[str, Any] = {}
        for k in ("workLocationOption", "work_location_option",
                  "locationFlexibility", "location_flexibility",
                  "category", "team", "businessUnit", "business_unit",
                  "skills", "yearsOfExperience", "employmentType"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=(
                item.get("name")
                or item.get("posting_name")
                or item.get("title")
                or "Untitled"
            ),
            company=self.company_name,
            ats_type=self.ats,
            ats_id=ats_id,
            location=_format_location(item),
            locations=_posting_places(item),
            workplace_type=next((mode for key in _WORKPLACE_KEYS if (mode := workplace_type(item.get(key)))), None),
            is_remote=_extract_remote(item),
            department=item.get("department"),
            requisition_id=str(requisition_id) if requisition_id else None,
            description=_strip_html(item.get("job_description") or "") or None,
            posted_at=_parse_ts(
                item.get("postedTs")
                or item.get("creationTs")
                or item.get("t_create")
                or item.get("t_update")
            ),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


async def _gather_cancel_on_error(*coros: Any) -> None:
    """Like ``asyncio.gather`` but cancels and drains siblings when one task
    raises, so no in-flight task keeps mutating shared state after the first
    error propagates."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class _WAFBlocked(Exception):  # noqa: N818
    """Internal signal that httpx hit a 403 — caller decides whether to
    fall back to httpcloak or surface the error."""

    def __init__(self, company_name: str, start: int) -> None:
        super().__init__(
            f"Eightfold ({company_name}) blocked by WAF at start={start}"
        )
        self.company_name = company_name
        self.start = start


class _EdgeBlocked(Exception):  # noqa: N818
    """Internal signal that a listing page met the edge's 405 block."""


class _RateLimited(Exception):  # noqa: N818
    """Internal signal that a paced listing page answered 429."""

    def __init__(self, generation: int, retry_after: float | None) -> None:
        super().__init__("rate-limited (429)")
        self.generation = generation
        self.retry_after = retry_after


class _ListingPace:
    """Spaces the listing requests of one board once it has answered 429.

    The limit counts the board's requests, so a 429 pauses every request of
    the board, not just the one that met it.
    """

    def __init__(self, company_name: str) -> None:
        self.company_name = company_name
        self.interval = 0.0
        self._next_slot = 0.0
        self._generation = 0  # number of pauses so far
        self._strikes = 0

    async def wait(self) -> int:
        """Wait for this request's slot; returns the pause generation it is
        sent under, for ``back_off``."""
        loop = asyncio.get_running_loop()
        while True:
            generation = self._generation
            now = loop.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
            if slot > now:
                await asyncio.sleep(slot - now)
            # A pause that began while this request waited for its slot
            # moves it behind the pause.
            if generation == self._generation:
                return generation

    def note_served(self, generation: int) -> None:
        # A page sent before the latest pause says nothing about how it ends.
        if generation == self._generation:
            self._strikes = 0

    def back_off(self, *, generation: int, retry_after: float | None, start: int) -> None:
        """Pause the board after a 429; raises once the board keeps refusing."""
        if generation != self._generation:
            # Sent before the latest pause began: that pause already covers it.
            return
        longest = max(RATE_LIMIT_COOLOFF_DELAYS)
        if retry_after is not None and retry_after > longest:
            raise ScraperError(
                f"Eightfold ({self.company_name}) rate-limited (429) at start={start}, "
                f"Retry-After {retry_after:.0f}s"
            )
        if self._strikes == len(RATE_LIMIT_COOLOFF_DELAYS):
            raise ScraperError(
                f"Eightfold ({self.company_name}) rate-limited (429) at start={start} "
                f"after waiting {sum(RATE_LIMIT_COOLOFF_DELAYS):.0f}s"
            )
        delay = max(RATE_LIMIT_COOLOFF_DELAYS[self._strikes], retry_after or 0.0)
        if self._strikes == 0:
            self.interval = min(
                MAX_RATE_LIMITED_INTERVAL, max(RATE_LIMITED_INTERVAL, self.interval * 2)
            )
        self._strikes += 1
        self._next_slot = asyncio.get_running_loop().time() + delay
        self._generation += 1
        import logging
        logging.getLogger(__name__).warning(
            "Eightfold (%s) rate-limited (429) at start=%d; pausing %.0fs, "
            "then one listing request every %.1fs",
            self.company_name, start, delay, self.interval,
        )


class _SmartApplyRequired(Exception):  # noqa: N818
    """Internal signal that PCSX is disabled for a SmartApply tenant."""

    def __init__(self, company_name: str) -> None:
        super().__init__(f"Eightfold ({company_name}) requires SmartApply API")
        self.company_name = company_name


def _is_pcsx_unavailable(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    message = str(payload.get("message") or "").lower()
    return "pcsx is not enabled" in message or "not authorized for pcsx" in message


def _retry_after_header(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return float(retry_after)
    return None


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Delay before retrying a 429: honour a numeric ``Retry-After`` header,
    else back off exponentially from ``RETRY_BASE_DELAY``."""
    retry_after = _retry_after_header(response)
    if retry_after is not None:
        return retry_after
    return float(RETRY_BASE_DELAY * (2 ** attempt))


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _detail_retry_delay(response: httpx.Response, attempt: int) -> float:
    """429 honours ``Retry-After`` / exponential; 5xx backs off linearly."""
    if response.status_code == 429:
        return _retry_after_seconds(response, attempt)
    return float(RETRY_BASE_DELAY * attempt)


def _position_id_from_url(url: object) -> str | None:
    """Pull the numeric position id out of a Eightfold job URL.

    URLs look like ``https://{tenant}.eightfold.ai/careers/job/563087414352251``
    (or the same path on the custom-domain variant). The trailing
    segment is the numeric id.
    """
    if not url:
        return None
    s = str(url).rstrip("/")
    tail = s.rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure.
    Replaces the legacy _strip_html/_html_to_text path for descriptions
    only — title/company/salary fields still use the strip variant."""
    import html as _h
    if not isinstance(value, str):
        return None
    out = _h.unescape(value).strip()
    if not out:
        return None
    return out[:cap]


def _strip_html(text: str) -> str:
    text = _TAG_RE_EF.sub(" ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _format_location(item: dict[str, Any]) -> str | None:
    """Eightfold returns `locations` / `standardizedLocations` as string lists
    (e.g. ``"United States, Washington, Redmond"``). Older tenants use dicts.
    A multi-location posting lists every office; entries already contain
    commas, so they are joined with ``; `` (as paycom's and jobvite's are,
    see ``location`` in docs/JOB_SCHEMA.md)."""
    for key in ("standardizedLocations", "locations"):
        locs = item.get(key) or []
        if not isinstance(locs, list):
            continue
        names: list[str] = []
        for loc in locs:
            if isinstance(loc, dict):
                loc = _dict_location_label(loc)
            if isinstance(loc, str) and loc.strip():
                names.append(loc.strip())
        if names:
            return "; ".join(dict.fromkeys(names))
    primary = item.get("primaryLocation") or item.get("primary_location")
    if isinstance(primary, dict):
        return primary.get("city") or primary.get("country")
    if isinstance(primary, str):
        return primary
    return None


def _dict_location_label(loc: dict[str, Any]) -> str | None:
    """``City, Country`` from a dict entry, else whichever of the two is
    filled, else its ``name``. The country is kept so that two offices in
    cities of the same name stay two entries; blank fields are skipped."""
    def text(key: str) -> str:
        value = loc.get(key)
        return value.strip() if isinstance(value, str) else ""

    return ", ".join(part for part in (text("city"), text("country")) if part) or text("name") or None


def _posting_places(item: dict[str, Any]) -> list[JobLocation] | None:
    primary = item.get("primaryLocation") or item.get("primary_location")
    primary_label = _dict_location_label(primary) if isinstance(primary, dict) else primary

    def place(value: object) -> JobLocation | None:
        if isinstance(value, dict):
            label = _dict_location_label(value)
            return job_location(label, city=value.get("city"), region=value.get("state"),
                                country=value.get("country"), primary=True if label == primary_label else None)
        return job_location(value, primary=True if value == primary_label else None)

    for key in ("standardizedLocations", "locations"):
        entries = item.get(key)
        if isinstance(entries, list) and (places := distinct_places(place(value) for value in entries)):
            return places
    return distinct_places([place(primary)])


_WORKPLACE_KEYS = ("workLocationOption", "work_location_option", "locationFlexibility", "location_flexibility")


def _extract_remote(item: dict[str, Any]) -> bool | None:
    """Eightfold encodes remote/hybrid in `workLocationOption` (string) or
    `locationFlexibility` (string). Common values: 'Remote', 'Hybrid',
    'Onsite', 'Up to 100% work from home'. We map the obvious ones; unknowns
    fall through to None so consumers can still tell "we don't know" from
    "we know it's not remote"."""
    for key in _WORKPLACE_KEYS:
        value = item.get(key)
        if not isinstance(value, str):
            continue
        v = value.strip().lower()
        if not v:
            continue
        if "remote" in v or "work from home" in v or "wfh" in v:
            return True
        if v in {"onsite", "on-site", "in office", "in-office", "office"}:
            return False
    return None


def _parse_ts(value: int | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromtimestamp(value / 1000 if value > 1e10 else value, tz=UTC)
    except (ValueError, OSError):
        return None
