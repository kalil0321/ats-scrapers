"""Bundesagentur für Arbeit (German federal employment agency) scraper.

Single largest open job source we cover: ~1M+ active postings across
every German employer that lists with the agency. The portal at
``arbeitsagentur.de`` exposes a public unauthenticated JSON API that
the official frontend consumes:

    GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs
        ?size=100&page={1..100}
    Header: X-API-Key: jobboerse-jobsuche

The API caps pagination at ``size × page = 10,000`` results per query
(``size=100, page=100``). Past that limit, the server returns 400.

To collect the full ~1M jobs we subdivide *recursively* by orthogonal
facets in priority order: ``berufsfeld`` (144 categories) →
``arbeitszeit`` (5 work-time buckets, e.g. ``vz``/``tz``) →
``zeitarbeit`` (2 — temp work yes/no) → ``befristung`` (3 — permanent /
fixed-term / vocational). At each level we only descend if the bucket
still exceeds the 10k cap. Empirically this is enough to break every
oversize category into <10k leaves.

The earlier version subdivided by Bundesland names, but the API's
``arbeitsort`` filter expects *city* names (e.g. ``"Berlin"``,
``"München"``), not states (``"Bayern"`` returns 0) — that bug capped
output at ~301k.

A subsequent 4-facet version (``berufsfeld → arbeitszeit → zeitarbeit →
befristung``) still capped near ~301-500k because the tail facets are
heavily skewed (~84% in the dominant bucket each), so the worst leaf —
Verkauf + vz + false + befristung=3 — still held 56k jobs against a
10k cap. The current 6-facet recursion adds ``eintrittsdatum`` (24
month windows) and ``arbeitgeber`` (top-100 employers per leaf), which
is enough to drive every dominant leaf below 10k.

Single-tenant scraper: ``company_slug`` is informational and ignored.
The output rows carry the German employer name as ``company`` so the
publisher's cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any

logger = logging.getLogger(__name__)

API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
API_KEY = "jobboerse-jobsuche"  # Public key shared by the official frontend.
PAGE_SIZE = 100
PAGE_LIMIT = 100  # size × page caps at 10,000 → max page=100 at size=100.
PAGINATION_CAP = PAGE_SIZE * PAGE_LIMIT
# The 6-facet recursion issues 10k+ requests for a full scrape. The
# arbeitsagentur API has an Akamai-style WAF that returns 403 under
# burst load. A shared global semaphore at 2 + sequential page fan-out
# within each leaf keeps the request pace below the WAF threshold while
# still parallelizing across the recursion tree.
MAX_CONCURRENCY = 2
MAX_RETRIES = 6
RETRY_BASE_DELAY = 2.0
RETRY_JITTER = 0.5  # ± fraction added to each backoff so concurrent
# retries don't synchronize and re-trigger the WAF in lockstep.

# Subdivision facets in priority order. Each facet's ``counts`` dict
# enumerates the available values for that filter — we read those at
# query time so the scraper survives taxonomy churn.
#
# Facet ordering matters: API responses cap at 10k results, so we want
# the highest-cardinality / least-skewed facets applied first. The
# tail (arbeitszeit/zeitarbeit/befristung) is heavily skewed (~84% in
# the dominant bucket each), which is why berufsfeld + the original
# 4 facets weren't enough — the worst leaf (Verkauf+vz+false+
# befristung=3) still held 56k jobs. eintrittsdatum (24 monthly start
# windows + a "10_01_01-now" catch-all) and arbeitgeber (top-100
# employers per leaf) are the levers that finally crack the dominant
# leaves.
_SUBDIVISION_FACETS = (
    "berufsfeld",      # 144 buckets, full coverage
    "eintrittsdatum",  # 24 month windows; multi-tag (sum > total) so dedup is essential
    "arbeitszeit",     # 5 work-time codes, multi-tag
    "befristung",      # 3 contract types
    "zeitarbeit",      # 2 (temp work y/n)
    "arbeitgeber",     # top-100 employers per leaf — last-resort partition
)
MAX_SUBDIVISION_DEPTH = len(_SUBDIVISION_FACETS)


class _PageFetchExhaustedError(ScraperError):
    """Internal signal that ``_fetch_page`` exhausted its retry budget on
    a *transient* failure class (persistent 403 / 429 / 5xx, or a network
    error that didn't resolve before MAX_RETRIES).

    Distinguished from ``ScraperError`` because the soft-fail callers
    (``_exhaust_query`` / ``_fan_out_pages``) only want to swallow this
    specific case — not contract breaks (401, 404, non-retryable status,
    malformed JSON), which still raise plain ``ScraperError`` and crash
    the scrape so an operator notices instead of silently undercounting.
    """


@ScraperRegistry.register(ATSType.BUNDESAGENTUR)
class BundesagenturScraper(BaseScraper):
    """Bundesagentur für Arbeit (DE) jobs API. Single-source scraper —
    ``company_slug`` is unused."""

    ats = ATSType.BUNDESAGENTUR

    def fetch(self) -> list[Job]:
        """Legacy in-memory fetch — accumulates the full corpus into a
        list. At ~750 k jobs that's a few GB of Job objects in RAM,
        sitting alongside other cron jobs. Prefer :meth:`fetch_stream`
        from cron contexts that write straight to disk."""
        return asyncio.run(self._fetch_async())

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Memory profile: ~200 MB regardless of corpus size — only the
        ``seen`` ID set + a bounded in-flight queue stays resident.
        Shares its fan-out + dedup logic with :meth:`_fetch_async` by
        plugging a queue-pushing ``on_job`` callback into it. The
        consumer iterator yields each job as it lands so callers
        (e.g. :func:`scripts.run_pipeline.run`) can write straight
        to a CSV writer without ever holding the full corpus in RAM.

        Termination uses an ``asyncio.Event`` rather than a queue
        sentinel: the consumer polls ``queue.get`` with a 500 ms
        timeout and checks ``producer_done`` between polls. This
        avoids the deadlock that a bounded-queue sentinel-put would
        introduce if the consumer ever stops draining (cubic PR #69
        P1) and keeps producer cleanup non-blocking.
        """
        queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=2000)
        producer_done = asyncio.Event()

        async def on_job(job: Job) -> None:
            await queue.put(job)

        async def producer() -> None:
            try:
                await self._fetch_async(on_job=on_job)
            finally:
                producer_done.set()

        task = asyncio.create_task(producer())
        try:
            while True:
                if producer_done.is_set() and queue.empty():
                    await task  # propagate any producer exception
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                yield item
        except BaseException:
            task.cancel()
            raise

    async def _fetch_async(
        self,
        *,
        on_job: Callable[[Job], Awaitable[None]] | None = None,
    ) -> list[Job]:
        """Drive the recursive query fan-out + dedup.

        Two modes:

        - ``on_job is None`` (default): accumulate every deduped job
          into a list and return it. Used by :meth:`fetch` for small-
          corpus / test paths.

        - ``on_job`` set to an async callback: dispatch each deduped
          job to the callback instead of accumulating. Used by
          :meth:`fetch_stream` so the queue consumer can write jobs
          to disk as they land; the in-memory footprint drops to just
          the ``seen`` ID set (~30 MB at full corpus). Returns an
          empty list in this mode.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
            # Dedup under the lock, then dispatch to the sink outside
            # the lock so a slow ``on_job`` callback can't serialise
            # every absorbing task on the lock.
            new_jobs: list[Job] = []
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    new_jobs.append(job)
            if on_job is not None:
                for job in new_jobs:
                    await on_job(job)
            else:
                all_jobs.extend(new_jobs)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            await self._exhaust_query(
                client, sem, base_params={}, depth=0, absorb=absorb,
            )
        return all_jobs

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        depth: int,
        absorb,
    ) -> None:
        """Recursively pull all jobs matching ``base_params``.

        Pagination caps at 10k. If the query exceeds that, pick the next
        unused subdivision facet and split. ``depth`` bounds the
        recursion: berufsfeld → arbeitszeit → zeitarbeit → befristung.
        """
        try:
            first = await self._fetch_page(
                client, sem, params={**base_params, "size": 1, "page": 1},
            )
        except _PageFetchExhaustedError as exc:
            # Probe exhausted retries on a transient class (persistent WAF
            # block or a network error that didn't resolve in time). We
            # soft-fail this *one* subtree only — sibling buckets keep
            # going. Probe failures must never silently look like
            # ``maxErgebnisse=0`` (that would drop the whole subtree and
            # at depth=0 the entire scrape) so we log loudly.
            #
            # Non-transient failures (401/404 contract breaks, malformed
            # JSON, etc.) raise plain ``ScraperError`` from ``_fetch_page``
            # and propagate up here uncaught — those crash the scrape
            # rather than produce a silent undercount.
            logger.warning(
                "Bundesagentur probe failed for params=%s depth=%d — "
                "subtree skipped, output will undercount: %s",
                base_params, depth, exc,
            )
            return
        total = int(first.get("maxErgebnisse") or 0)
        if total == 0:
            return
        # Page-1 hits are already paid for — absorb them rather than re-fetch.
        await absorb(first.get("stellenangebote") or [])

        if total <= PAGINATION_CAP:
            await self._fan_out_pages(
                client, sem,
                base_params=base_params, total=total, absorb=absorb,
            )
            return

        # Above the cap — pick a subdivision facet not already in
        # base_params, then split.
        applied = set(base_params.keys())
        facet_name: str | None = None
        for f in _SUBDIVISION_FACETS:
            if f not in applied:
                facet_name = f
                break

        if facet_name is None or depth >= MAX_SUBDIVISION_DEPTH:
            # Out of facets — fall through and accept the 10k cap.
            await self._fan_out_pages(
                client, sem,
                base_params=base_params, total=PAGINATION_CAP, absorb=absorb,
            )
            return

        facets = first.get("facetten") or {}
        bucket_counts = _bucket_counts(facets, facet_name)
        if not bucket_counts:
            await self._fan_out_pages(
                client, sem,
                base_params=base_params, total=PAGINATION_CAP, absorb=absorb,
            )
            return

        async def child_bucket(value: str, count: int) -> None:
            if count == 0:
                return
            child_params = {**base_params, facet_name: value}
            await self._exhaust_query(
                client, sem,
                base_params=child_params, depth=depth + 1, absorb=absorb,
            )

        await asyncio.gather(
            *(child_bucket(v, c) for v, c in bucket_counts.items())
        )

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        absorb,
    ) -> None:
        # We can fetch ``ceil(total / PAGE_SIZE)`` pages, capped at PAGE_LIMIT.
        page_count = min((total + PAGE_SIZE - 1) // PAGE_SIZE, PAGE_LIMIT)

        # Sequential page fan-out within a single leaf — the recursion
        # tree provides cross-leaf parallelism via the global semaphore.
        # Bursting 50+ page requests for one leaf was the WAF trigger we
        # saw at concurrency=3.
        for page in range(1, page_count + 1):
            params = {**base_params, "size": PAGE_SIZE, "page": page}
            try:
                payload = await self._fetch_page(client, sem, params=params)
            except _PageFetchExhaustedError as exc:
                # Page-level soft-fail: lose at most ``PAGE_SIZE`` jobs from
                # this one page; keep working on the rest of the leaf and
                # the rest of the tree. Bounded loss from transient WAF /
                # network exhaustion is acceptable. (Probe failures hit
                # the same class but ``_exhaust_query`` handles them
                # separately because they affect a whole subtree.)
                #
                # Non-transient failures (contract breaks, bad JSON) raise
                # plain ``ScraperError`` and propagate uncaught — better
                # to crash than to silently undercount.
                logger.warning(
                    "Bundesagentur page %d/%d failed for params=%s — "
                    "page skipped (~%d jobs lost): %s",
                    page, page_count, base_params, PAGE_SIZE, exc,
                )
                continue
            await absorb(payload.get("stellenangebote") or [])

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    r = await client.get(
                        API_URL,
                        params=params,
                        headers={
                            "X-API-Key": API_KEY,
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Bundesagentur returned non-JSON for {params}: {exc}"
                    ) from exc
            if r.status_code == 400:
                # Past pagination cap — return empty so caller stops.
                return {"stellenangebote": [], "maxErgebnisse": 0}
            # 403 here is a transient Akamai/WAF rate-limit, not a real
            # auth failure (the API key never expires); back off and retry.
            if r.status_code in (403, 429) or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    # Persistent WAF/server failure — raise the *narrowed*
                    # ``_PageFetchExhaustedError`` so callers can soft-fail it
                    # specifically. ``_exhaust_query`` treats this as a
                    # subtree-loss (logs + skips); ``_fan_out_pages`` treats
                    # it as a page-loss (logs + continues). We must NOT
                    # silently return an empty payload here: that would be
                    # indistinguishable from a real ``maxErgebnisse=0``
                    # response and would silently abandon the whole subtree
                    # whenever a probe gets WAF-blocked.
                    raise _PageFetchExhaustedError(
                        f"Bundesagentur returned {r.status_code} for {params} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = r.headers.get("Retry-After")
                base = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                # Jitter: ± up to RETRY_JITTER × base, so concurrent retries
                # don't synchronize and re-trigger the WAF together.
                delay = base * (1 + random.uniform(-RETRY_JITTER, RETRY_JITTER))
                await asyncio.sleep(delay)
                continue
            # Non-retryable status (401 auth break, 404 endpoint moved,
            # 4xx other than 403/429, etc.) — these are contract breaks,
            # not transient. Raise plain ``ScraperError`` so callers do
            # NOT swallow it as a soft-fail; the scrape crashes loudly
            # rather than silently producing a wholesale undercount.
            raise ScraperError(
                f"Bundesagentur returned {r.status_code} for {params}: "
                f"{r.text[:120]}"
            )
        # Network errors exhausted the retry budget — same transient class
        # as persistent WAF, so callers can soft-fail just this fetch.
        raise _PageFetchExhaustedError(
            f"Bundesagentur exhausted retries for {params}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("refnr") or "").strip()
        title = (item.get("titel") or item.get("beruf") or "").strip()
        if not ats_id or not title:
            return None
        location = _format_location(item.get("arbeitsort"))
        company = (item.get("arbeitgeber") or "Bundesagentur").strip() or "Bundesagentur"

        # Each posting has a deterministic public URL on jobsuche.arbeitsagentur.de.
        # The detail endpoint expects base64(refnr); the human URL accepts refnr.
        url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ats_id}"

        # Bundesagentur exposes ``arbeitszeit`` as the work-time bucket
        # — ``vz`` (Vollzeit / full-time), ``tz`` (Teilzeit / part-time),
        # ``mj`` (Minijob / contract-style), ``ho`` (Home office),
        # ``saison`` (Seasonal), ``ne`` (Nebenjob / side gig),
        # ``selb`` (Selbständig / self-employed). Map the canonical ones.
        arbeitszeit = item.get("arbeitszeit")
        commitment: str | None = None
        employment_type: str | None = None
        if isinstance(arbeitszeit, str) and arbeitszeit.strip():
            commitment = _ARBEITSZEIT_LABELS.get(
                arbeitszeit.strip().lower(), arbeitszeit.strip(),
            )
            employment_type = _ARBEITSZEIT_TO_EMPLOYMENT_TYPE.get(
                arbeitszeit.strip().lower(),
            )
        # ``zeitarbeit=true`` (temp-agency placement) is unambiguous —
        # surface as TEMPORARY when the time-type doesn't disambiguate.
        if not employment_type and item.get("zeitarbeit") is True:
            employment_type = "TEMPORARY"
        # ``befristung=2`` indicates fixed-term in the BA taxonomy.
        if not employment_type and str(item.get("befristung") or "") == "2":
            employment_type = "CONTRACT"

        # ``ho`` (Home office) is the only explicit remote signal.
        is_remote = True if isinstance(arbeitszeit, str) and arbeitszeit.lower() == "ho" else None

        # ``berufsfeld`` is a high-level domain (Pedagogik / IT / Sales)
        # — closest match to a department facet.
        berufsfeld = item.get("berufsfeld")
        department = (
            berufsfeld.strip()
            if isinstance(berufsfeld, str) and berufsfeld.strip()
            else None
        )

        # Industry / sector → ``team`` (the closest analog the API exposes).
        branche = item.get("branche")
        team = (
            branche.strip()
            if isinstance(branche, str) and branche.strip()
            and branche.strip() != department
            else None
        )

        raw: dict[str, Any] = {}
        for k in ("branche", "berufsfeld", "befristung", "zeitarbeit",
                  "arbeitgeberHashId", "kundennummerHash", "externeUrl",
                  "arbeitszeit", "modifikationsTimestamp"):
            v = item.get(k)
            if v not in (None, ""):
                raw[k] = v

        externe_url = item.get("externeUrl")
        apply_url = externe_url if isinstance(externe_url, str) and externe_url.startswith("http") else None

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.BUNDESAGENTUR,
            ats_id=ats_id,
            location=location,
            is_remote=is_remote,
            department=department,
            team=team,
            employment_type=employment_type,
            commitment=commitment,
            apply_url=apply_url,
            requisition_id=item.get("hashId") or None,
            posted_at=_parse_iso(item.get("eintrittsdatum") or item.get("aktuelleVeroeffentlichungsdatum")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# Bundesagentur's ``arbeitszeit`` is a single-letter-ish code; the
# values are stable across the API surface.
_ARBEITSZEIT_LABELS = {
    "vz": "Vollzeit",
    "tz": "Teilzeit",
    "mj": "Minijob",
    "ho": "Home office",
    "saison": "Saisonarbeit",
    "ne": "Nebenjob",
    "selb": "Selbständig",
    "snw": "Schicht/Nacht/Wochenende",
}
_ARBEITSZEIT_TO_EMPLOYMENT_TYPE = {
    "vz": "FULL_TIME",
    "tz": "PART_TIME",
    "ho": "FULL_TIME",
    "mj": "PART_TIME",
    "ne": "PART_TIME",
    "saison": "TEMPORARY",
    "selb": "CONTRACT",
}


def _bucket_counts(facets: dict[str, Any], facet_name: str) -> dict[str, int]:
    """Return ``{value_label: count}`` for a given facet, or ``{}`` if the
    response doesn't expose it. The API's ``facetten`` dict maps each
    facet name to ``{"counts": {label: n, ...}, "maxCount": ...}``."""
    if not isinstance(facets, dict):
        return {}
    facet = facets.get(facet_name)
    counts = facet.get("counts") if isinstance(facet, dict) else None
    if not isinstance(counts, dict):
        return {}
    return {str(k): int(v) for k, v in counts.items() if int(v) > 0}


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        str(value[k]).strip()
        for k in ("ort", "region", "land")
        if isinstance(value.get(k), str) and value.get(k).strip() and value.get(k) != "null"
    ]
    return ", ".join(parts) or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
