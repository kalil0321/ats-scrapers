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


@ScraperRegistry.register(ATSType.BUNDESAGENTUR)
class BundesagenturScraper(BaseScraper):
    """Bundesagentur für Arbeit (DE) jobs API. Single-source scraper —
    ``company_slug`` is unused."""

    ats = ATSType.BUNDESAGENTUR

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    all_jobs.append(job)

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
        first = await self._fetch_page(
            client, sem, params={**base_params, "size": 1, "page": 1},
        )
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
            payload = await self._fetch_page(client, sem, params=params)
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
                    # Soft-fail: log + drop this leaf so the scraper still
                    # completes the rest. Crashing on a single persistent
                    # WAF block would lose ~95% of the work the recursion
                    # already finished.
                    logger.warning(
                        "Bundesagentur returned %s after %d retries for %s — "
                        "skipping this leaf (output will undercount).",
                        r.status_code, MAX_RETRIES, params,
                    )
                    return {"stellenangebote": [], "maxErgebnisse": 0}
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
            raise ScraperError(
                f"Bundesagentur returned {r.status_code} for {params}: "
                f"{r.text[:120]}"
            )
        raise ScraperError(
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

        # Bundesagentur exposes ``arbeitszeit`` as the work-time bucket (vz/tz/...)
        # and a separate ``branche`` (industry). ``hashId`` is the durable
        # employer-side requisition identifier — same value across cross-postings.
        commitment = item.get("arbeitszeit") or None
        raw: dict[str, Any] = {}
        for k in ("branche", "berufsfeld", "befristung", "zeitarbeit",
                  "arbeitgeberHashId", "kundennummerHash", "externeUrl",
                  "modifikationsTimestamp"):
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
            commitment=commitment,
            apply_url=apply_url,
            requisition_id=item.get("hashId") or None,
            posted_at=_parse_iso(item.get("eintrittsdatum") or item.get("aktuelleVeroeffentlichungsdatum")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


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
