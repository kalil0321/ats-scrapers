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
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

PAGE_SIZE = 10  # Eightfold's server-fixed page size.
MAX_CONCURRENCY_HTTPX = 12  # PCSX comfortably handles this; raise carefully.
MAX_CONCURRENCY_HTTPCLOAK = 4  # browser-fingerprint clients are heavier per-request
DETAIL_CONCURRENCY = 8  # per-tenant detail-page enrichment cap
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5  # seconds; exponential for 429, linear for 5xx
SLOW_REQUEST_THRESHOLD = 5.0  # log a warning for any single request slower than this

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
        base_url: str | None = None,
        domain: str | None = None,
        company_name: str | None = None,
        job_url_host: str | None = None,
        client_kind: ClientKind = "auto",
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.base_url = (base_url or f"https://{company_slug}.eightfold.ai").rstrip("/")
        self.domain = domain or f"{company_slug}.com"
        self.company_name = company_name or company_slug.replace("-", " ").title()
        # Some tenants (notably Microsoft) serve the API on one host but
        # render job URLs on a different one. Default to the API host.
        self.job_url_host = (job_url_host or self.base_url).rstrip("/")
        self.client_kind: ClientKind = client_kind

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    # --- async pipeline -------------------------------------------------

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []

        if self.client_kind == "httpcloak":
            await asyncio.to_thread(self._fetch_via_httpcloak_sync, seen, all_jobs)
            return all_jobs

        # httpx or auto: try httpx first
        try:
            await self._fetch_via_httpx(seen, all_jobs)
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
        ) as client:
            first = await self._fetch_page_httpx(client, start=0)
            self._collect(first.get("positions") or [], seen, all_jobs)
            count = int(first.get("count") or 0)
            if count <= PAGE_SIZE:
                return
            offsets = list(range(PAGE_SIZE, count, PAGE_SIZE))
            sem = asyncio.Semaphore(MAX_CONCURRENCY_HTTPX)

            async def task(offset: int) -> None:
                async with sem:
                    page = await self._fetch_page_httpx(client, start=offset)
                    self._collect(page.get("positions") or [], seen, all_jobs)

            await asyncio.gather(*(task(o) for o in offsets))

            # Detail enrichment — pull jobDescription from the public
            # `position_details` XHR endpoint per job. PCSX handles the
            # higher request volume well; the search calls were ~10
            # pages × 12 concurrency, the detail pass is per-job but
            # still fits inside the same WAF budget.
            if all_jobs:
                detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_position_details(client, detail_sem, j)
                    for j in all_jobs
                ))

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
        # Use the position id (matches search response's ``id``, not
        # ``displayJobId``). The id is encoded in the position URL.
        position_id = _position_id_from_url(job.url)
        if not position_id:
            return
        async with sem:
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
                return
        if response.status_code != 200:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        data = payload.get("data") or {}
        desc_html = data.get("jobDescription")
        if isinstance(desc_html, str) and desc_html.strip() and not job.description:
            job.description = _strip_html(desc_html)[:12_000] or None

    async def _fetch_page_httpx(
        self, client: httpx.AsyncClient, *, start: int
    ) -> dict[str, Any]:
        """One page with retry on 429/5xx (ported from the legacy Microsoft
        scraper, where ~1% of requests hit transient 502s on Eightfold)."""
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
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
                return response.json().get("data") or {}
            if response.status_code == 403:
                raise _WAFBlocked(self.company_name, start)
            if response.status_code == 429:  # rate-limited
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) rate-limited (429) at "
                        f"start={start} after {MAX_RETRIES} retries"
                    )
                # Honour Retry-After if present, else exponential
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            if 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Eightfold ({self.company_name}) returned "
                        f"{response.status_code} at start={start} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            # Non-retryable status (404, etc.)
            raise ScraperError(
                f"Eightfold ({self.company_name}) returned {response.status_code} at start={start}"
            )

        # Defensive — loop should always raise or return.
        raise ScraperError(
            f"Eightfold ({self.company_name}) exhausted retries at start={start}: {last_exc}"
        )

    # --- httpcloak path (sync, but parallelized via to_thread) ----------

    def _fetch_via_httpcloak_sync(
        self, seen: set[str], all_jobs: list[Job]
    ) -> None:
        try:
            import httpcloak  # noqa: F401  — availability check
        except ImportError as exc:
            raise ScraperError(
                "httpcloak required for this tenant; install with "
                "`pip install httpcloak` or jobhive[scrapers]"
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
            )
        except Exception as exc:  # httpcloak may raise misc subclasses
            raise ScraperError(
                f"Eightfold ({self.company_name}) httpcloak failed at start={start}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise ScraperError(
                f"Eightfold ({self.company_name}) httpcloak returned {response.status_code} at start={start}"
            )
        return response.json().get("data") or {}

    # --- shared helpers -------------------------------------------------

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
            item.get("displayJobId") or item.get("id") or item.get("atsJobId") or ""
        )
        position = item.get("positionUrl") or ""
        if position.startswith("/"):
            url = f"{self.job_url_host}{position}"
        elif position:
            url = position
        else:
            url = f"{self.job_url_host}/careers/job/{ats_id}"

        # Eightfold typically wraps a Workday or other underlying ATS — its
        # ``atsJobId`` / ``displayJobId`` is the upstream requisition id and
        # collides with the underlying ATS's bulletFields[0]. That's the
        # signal the cross-ATS dedup pass uses (Pass 3).
        requisition_id = item.get("atsJobId") or item.get("displayJobId")

        raw: dict[str, Any] = {}
        for k in ("workLocationOption", "locationFlexibility",
                  "category", "team", "businessUnit", "skills",
                  "yearsOfExperience", "employmentType"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=item.get("name") or item.get("title") or "Untitled",
            company=self.company_name,
            ats_type=self.ats,
            ats_id=ats_id,
            location=_format_location(item),
            is_remote=_extract_remote(item),
            department=item.get("department"),
            requisition_id=str(requisition_id) if requisition_id and str(requisition_id) != ats_id else None,
            posted_at=_parse_ts(item.get("postedTs") or item.get("creationTs")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


class _WAFBlocked(Exception):  # noqa: N818
    """Internal signal that httpx hit a 403 — caller decides whether to
    fall back to httpcloak or surface the error."""

    def __init__(self, company_name: str, start: int) -> None:
        super().__init__(
            f"Eightfold ({company_name}) blocked by WAF at start={start}"
        )
        self.company_name = company_name
        self.start = start


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


def _strip_html(text: str) -> str:
    text = _TAG_RE_EF.sub(" ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _format_location(item: dict[str, Any]) -> str | None:
    """Eightfold returns `locations` / `standardizedLocations` as string lists
    (e.g. ``"United States, Washington, Redmond"``). Older tenants use dicts."""
    for key in ("standardizedLocations", "locations"):
        locs = item.get(key) or []
        if isinstance(locs, list) and locs:
            first = locs[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                return first.get("city") or first.get("country") or first.get("name")
    primary = item.get("primaryLocation") or item.get("primary_location")
    if isinstance(primary, dict):
        return primary.get("city") or primary.get("country")
    if isinstance(primary, str):
        return primary
    return None


def _extract_remote(item: dict[str, Any]) -> bool | None:
    """Eightfold encodes remote/hybrid in `workLocationOption` (string) or
    `locationFlexibility` (string). Common values: 'Remote', 'Hybrid',
    'Onsite', 'Up to 100% work from home'. We map the obvious ones; unknowns
    fall through to None so consumers can still tell "we don't know" from
    "we know it's not remote"."""
    for key in ("workLocationOption", "locationFlexibility"):
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
        return datetime.fromtimestamp(value / 1000 if value > 1e10 else value)
    except (ValueError, OSError):
        return None
