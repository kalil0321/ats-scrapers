"""Bundesagentur für Arbeit (German federal employment agency) scraper.

Single largest open job source we cover: ~1M+ active postings across
every German employer that lists with the agency. The portal at
``arbeitsagentur.de`` exposes a public unauthenticated JSON API that
the official frontend consumes:

    GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs
        ?size=100&page={1..100}
    Header: X-API-Key: jobboerse-jobsuche

The official frontend moved listings from v4 to v6 in August 2026. The v6
response uses ``ergebnisliste`` and renamed German field keys, while job
details remain on the v4 endpoint.

The API caps pagination at ``size × page = 10,000`` results per query
(``size=100, page=100``). Past that limit, the server returns 400.

To collect the full ~1M jobs we subdivide *recursively* using only facets
whose bucket counts form an exact, non-overlapping partition of the current
query. If the API exposes no such partition for an oversized leaf, we split
it by region (``wo=<Land> (Bundesland)`` for each German state, then
``wo=<Staat> (Land)`` for postings abroad), deduplicate by reference number,
and check that the union reaches the leaf's advertised total. A region that
is still oversized gets an overlapping cover from every official sort order
plus high-cardinality facets, accepted only when its unique row count
reaches the advertised total. Otherwise the scrape fails closed and
preserves the previous complete dataset.

The ``arbeitsort`` filter expects *city* names (e.g. ``"Berlin"``,
``"München"``); a state name there returns 0. A bare state name in ``wo``
can also resolve to a town (``"Hessen"`` is Hessen bei Halberstadt), so
regions always carry the ``(Bundesland)`` / ``(Land)`` suffix.

Single-tenant scraper: ``company_slug`` is informational and ignored.
The output rows carry the German employer name as ``company`` so the
publisher's cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
import random
import time
from collections import Counter
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Sequence,
)
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, EngagementType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

logger = logging.getLogger(__name__)

API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_URL_TEMPLATE = (
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{encoded_ref}"
)
API_KEY = "jobboerse-jobsuche"  # Public key shared by the official frontend.
PAGE_SIZE = 100
PAGE_LIMIT = 100  # size × page caps at 10,000 → max page=100 at size=100.
PAGINATION_CAP = PAGE_SIZE * PAGE_LIMIT

# ``angebotsart`` bucket codes, measured live on the v6 facet (the API uses
# numeric codes, NOT the labels its own frontend displays):
#   1  = ARBEIT (regular employment)
#   2  = SELBSTAENDIGKEIT (self-employment)
#   34 = PRAKTIKUM_TRAINEE (internships & trainee programs)
#   4  = AUSBILDUNG (vocational training)
ANGEBOTSART_PRAKTIKUM_TRAINEE = "34"
# The recursive fan-out issues 10k+ requests for a full scrape. The API's
# gateway answers 403 under burst load. A shared global semaphore at 2 +
# sequential page fan-out within each leaf keeps the request pace below that
# threshold while still parallelizing across the recursion tree.
MAX_CONCURRENCY = 2
MAX_RETRIES = 6
RETRY_BASE_DELAY = 2.0
RETRY_JITTER = 0.5  # ± fraction added to each backoff so concurrent
# retries don't synchronize and re-trigger the WAF in lockstep.
# The gateway refuses single requests with 403 while the requests around them
# go through, and accepts the same request again later. A refused page waits
# with backoff capped at RETRY_MAX_DELAY, about 16 minutes over
# MAX_BLOCKED_ATTEMPTS, without holding a request slot, so other pages go on.
# The scrape still fails closed when one page is refused that long or the run
# meets MAX_TRANSIENT_FAILURES, as it does when the API is down.
MAX_BLOCKED_ATTEMPTS = 14
RETRY_MAX_DELAY = 120.0
MAX_TRANSIENT_FAILURES = 5_000
# With several proxies, a route that met a 403, a 429 or a network error
# rests this long, and requests go through the next route in the meantime.
ROUTE_COOL_OFF = 300.0

# Candidate subdivision facets. At each node we only use a facet when its
# bucket counts sum exactly to that node's total, then pick the candidate
# whose largest bucket is smallest. This matters in v6: ``berufsfeld`` omits
# 7.9k uncategorized Ausbildung records at the root, while multi-valued facets
# such as ``arbeitszeit`` overlap. Treating either as an unconditional
# partition silently loses or double-counts jobs. Once ``angebotsart`` and
# ``ausbildungsart`` narrow the relevant branches, ``berufsfeld`` becomes
# exhaustive and is safe to use.
_SUBDIVISION_FACETS = (
    "angebotsart",
    "ausbildungsart",
    "berufsfeld",
    "beruf",
    "schulbildung",
    "befristung",
    "zeitarbeit",
    "pav",
    "quereinstieg",
    "externestellenboersen",
    "behinderung",
    "branche",
    "arbeitgeber",
)
MAX_SUBDIVISION_DEPTH = len(_SUBDIVISION_FACETS)

# The official frontend exposes these four sort orders. Different orderings
# surface different records before the API's hard 10k pagination cap.
_COVER_SORTS = (
    "relevanz",
    "veroeffdatum",
    "moddatum",
    "eintrittsdatum",
)
# These API-provided facets are intentionally allowed to overlap or omit a
# small tail. They are not trusted as partitions; they are only used to add
# records to a locally deduplicated cover whose final size is verified.
_COVER_FACETS = ("beruf", "arbeitsort", "weitereberufe")
MAX_COVER_TOTAL = 25_000
MAX_COVER_ATTEMPTS = 3
COVER_ABSORB_BATCH_SIZE = 500

# A posting with locations in several regions matches each of them, so the
# regions overlap and only work as a deduplicated cover.
_BUNDESLAENDER = (
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Thüringen",
)
DOMESTIC_REGION = "Deutschland (Land)"
# Countries the ``wo`` search resolves, most postings first. Only queried
# while a leaf still misses postings that are not in Germany.
_FOREIGN_COUNTRIES = (
    "Österreich",
    "Schweiz",
    "Niederlande",
    "Luxemburg",
    "Vereinigte Staaten",
    "Frankreich",
    "Belgien",
    "Spanien",
    "Portugal",
    "Italien",
    "Polen",
    "Großbritannien",
    "Dänemark",
    "Norwegen",
    "Schweden",
    "Griechenland",
    "Ungarn",
    "Bulgarien",
    "Indien",
    "Tschechien",
    "Liechtenstein",
    "China",
    "Rumänien",
    "Kroatien",
    "Irland",
    "Finnland",
    "Slowenien",
    "Slowakei",
    "Türkei",
    "Ukraine",
    "Vereinigte Arabische Emirate",
    "Vietnam",
    "Malta",
    "Japan",
    "Estland",
    "Lettland",
    "Litauen",
    "Zypern",
    "Island",
    "Serbien",
    "Kanada",
    "Australien",
    "Singapur",
    "Thailand",
    "Malaysia",
    "Usbekistan",
    "Russische Föderation",
    "Peru",
    "Nigeria",
    "Marokko",
    "Kosovo",
    "Kenia",
    "Kamerun",
    "Kambodscha",
    "Jordanien",
    "Grönland",
    "Ghana",
)
# Postings close and open while a region cover runs, so its union may fall
# this fraction short of the leaf total, and the total may move by as much,
# without failing the scrape.
REGION_COVER_TOLERANCE = 0.002


class _PageFetchExhaustedError(ScraperError):
    """Internal signal that ``_fetch_page`` exhausted its retry budget on
    a *transient* failure class (403 / 429 / 5xx or a network error that
    lasted MAX_BLOCKED_ATTEMPTS, or more than MAX_TRANSIENT_FAILURES of them
    in one run).

    Distinguished from ``ScraperError`` so logs preserve the retry-exhausted
    failure class. It is deliberately not swallowed: publishing a partial
    federal catalogue is less safe than preserving the previous complete run.
    """


class _RouteBody(httpx.AsyncByteStream):
    """A response body that rests its route when it breaks off."""

    def __init__(
        self, stream: httpx.AsyncByteStream, rest: Callable[[], None]
    ) -> None:
        self._stream = stream
        self._rest = rest

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._stream:
                yield chunk
        except httpx.TransportError:
            self._rest()
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


class _Routes(httpx.AsyncBaseTransport):
    """Send each request through the first route, in the given order, that
    is not resting. ``None`` is a direct connection."""

    def __init__(self, proxies: Sequence[str | None]) -> None:
        self._transports = [
            httpx.AsyncHTTPTransport(proxy=proxy) for proxy in proxies
        ]
        self._resting_until = [0.0] * len(self._transports)

    def _pick(self) -> int:
        now = time.monotonic()
        for index, until in enumerate(self._resting_until):
            if until <= now:
                return index
        return min(
            range(len(self._resting_until)),
            key=self._resting_until.__getitem__,
        )

    def _rest(self, index: int) -> None:
        self._resting_until[index] = time.monotonic() + ROUTE_COOL_OFF
        logger.debug("Bundesagentur route %d rests", index)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        index = self._pick()
        request.extensions["route"] = index
        try:
            response = await self._transports[index].handle_async_request(request)
        except httpx.TransportError:
            self._rest(index)
            raise
        if response.status_code in (403, 429):
            self._rest(index)
        # HTTPX reads the body after this returns.
        if isinstance(response.stream, httpx.AsyncByteStream):
            response.stream = _RouteBody(
                response.stream, functools.partial(self._rest, index)
            )
        return response

    async def aclose(self) -> None:
        for transport in self._transports:
            await transport.aclose()


@ScraperRegistry.register(ATSType.BUNDESAGENTUR)
class BundesagenturScraper(BaseScraper):
    """Bundesagentur für Arbeit (DE) jobs API. Single-source scraper —
    ``company_slug`` is unused."""

    ats = ATSType.BUNDESAGENTUR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        proxies: Sequence[str | None] | None = None,
        angebotsart: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        # Optional ``angebotsart`` facet filter, opt-in. The v6 API exposes
        # the facet with NUMERIC bucket codes, not labels (measured live):
        #   {"1": ARBEIT, "2": SELBSTAENDIGKEIT, "34": PRAKTIKUM_TRAINEE,
        #    "4": AUSBILDUNG}
        # Passing the label (e.g. ``angebotsart=Praktikum``) returns 0
        # results; the item field ``stellenangebotsart`` carries the enum
        # name. ``None`` (default) sends no filter — full catalogue, zero
        # behaviour change.
        self.angebotsart = angebotsart
        # Routes in order of preference. ``None`` is a direct connection.
        self.proxies: list[str | None] = list(proxies) if proxies else [proxy]
        # Retried 403 / 429 / 5xx responses by status, and network errors by
        # exception name, for the last run.
        self.transient_failures: Counter[str] = Counter()

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        copy = job.model_copy()
        async def run() -> str | None:
            async with self._client() as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(client, sem, copy)
            job.engagement_type = copy.engagement_type
            job.raw = copy.raw
            return copy.description

        return self._run_sync(run())

    def _client(self) -> httpx.AsyncClient:
        if self.proxies == [None]:
            return httpx.AsyncClient(timeout=self.timeout)
        return httpx.AsyncClient(
            timeout=self.timeout, transport=_Routes(self.proxies)
        )

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Memory profile: ~200 MB regardless of corpus size — only the
        ``seen`` ID set + a bounded in-flight queue stays resident.
        Prefer this over the in-memory :meth:`fetch` / :meth:`afetch`
        from cron contexts that write straight to disk — at ~750 k jobs
        the accumulated list is a few GB of Job objects in RAM.
        Shares its fan-out + dedup logic with :meth:`afetch` by
        plugging a queue-pushing ``on_job`` callback into it. The
        consumer iterator yields each job as it lands so callers can
        write straight to a CSV writer without ever holding the full
        corpus in RAM.

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
                await self.afetch(on_job=on_job)
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

    async def afetch(
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
                    if job is None or not job.ats_id or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    new_jobs.append(job)
            if self.include_descriptions:
                await asyncio.gather(*(
                    self._enrich_description(client, sem, job)
                    for job in new_jobs
                    if not job.description
                ))
            if on_job is not None:
                for job in new_jobs:
                    await on_job(job)
            else:
                all_jobs.extend(new_jobs)

        self.transient_failures = Counter()
        async with self._client() as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            base_params: dict[str, Any] = {}
            if self.angebotsart:
                # Opt-in facet filter (see ``__init__``): restricts the
                # whole recursive fan-out to one ``angebotsart`` bucket.
                base_params["angebotsart"] = self.angebotsart
            await self._exhaust_query(
                client, sem, base_params=base_params, depth=0, absorb=absorb,
            )
        if self.transient_failures:
            logger.warning(
                "Bundesagentur retried %d transient failures: %s",
                self.transient_failures.total(),
                dict(self.transient_failures),
            )
        return all_jobs

    async def _enrich_description(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        if not job.ats_id:
            return
        encoded = base64.b64encode(job.ats_id.encode()).decode()
        url = DETAIL_URL_TEMPLATE.format(encoded_ref=encoded)
        async with sem:
            try:
                response = await client.get(
                    url,
                    headers={
                        "X-API-Key": API_KEY,
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        try:
            detail = response.json()
        except ValueError:
            return
        if not isinstance(detail, dict):
            return
        terms = {k: detail[k] for k in _CONTRACT_FIELDS if detail.get(k) is not None}
        if terms:
            job.raw = {**(job.raw or {}), **terms}
            job.engagement_type = _engagement_type(job.raw)
        description = detail.get("stellenangebotsBeschreibung")
        if isinstance(description, str) and description.strip():
            job.description = description.strip()[:25_000]

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        depth: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        """Recursively pull all jobs matching ``base_params``.

        Pagination caps at 10k. If the query exceeds that, use an exact
        facet partition when available, then a region cover, then a verified
        sort-order cover. ``depth`` bounds recursive facet subdivision.
        """
        first = await self._fetch_page(
            client, sem, params={**base_params, "size": 1, "page": 1},
        )
        total = _result_total(first)
        if total == 0:
            return
        # Page-1 hits are already paid for — absorb them rather than re-fetch.
        await absorb(_result_items(first))

        if total <= PAGINATION_CAP:
            await self._fan_out_pages(
                client, sem,
                base_params=base_params, total=total, absorb=absorb,
            )
            return

        applied = set(base_params.keys())
        partition = _select_partition(first, total=total, applied=applied)
        if partition is None or depth >= MAX_SUBDIVISION_DEPTH:
            if "wo" not in base_params:
                await self._collect_region_cover(
                    client,
                    sem,
                    base_params=base_params,
                    total=total,
                    initial_payload=first,
                    depth=depth,
                    absorb=absorb,
                )
                return
            await self._collect_verified_cover(
                client,
                sem,
                base_params=base_params,
                total=total,
                absorb=absorb,
            )
            return
        facet_name, bucket_counts = partition

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

    async def _collect_region_cover(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        initial_payload: dict[str, Any],
        depth: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        """Split an oversized leaf without an exact facet partition by region.

        Every German state is exhausted like any other query. Countries
        abroad are then queried only until the union holds as many
        non-German postings as the leaf has. Postings located only as
        "Deutschland" match no state, so the official sort orders of the
        whole leaf fill what is still missing. Postings past the first 10,000
        of every sort order are then sought in the leaf's facet buckets that
        fit under the cap, while the union would otherwise be rejected. The
        union must reach the leaf's advertised total, less
        ``REGION_COVER_TOLERANCE`` for churn, and a second count at the end
        must stay within that tolerance of the first.
        """
        references: set[str] = set()

        async def record(items: list[dict[str, Any]]) -> None:
            references.update(
                reference
                for item in items
                if (reference := _item_reference(item))
            )
            await absorb(items)

        async def exhaust_region(region: str) -> None:
            await self._exhaust_query(
                client,
                sem,
                base_params={**base_params, "wo": region},
                depth=depth + 1,
                absorb=record,
            )

        domestic = await self._fetch_page(
            client,
            sem,
            params={**base_params, "wo": DOMESTIC_REGION, "size": 1, "page": 1},
        )
        abroad = total - _result_total(domestic)
        await asyncio.gather(
            *(exhaust_region(f"{land} (Bundesland)") for land in _BUNDESLAENDER)
        )
        found_in_germany = len(references)
        for country in _FOREIGN_COUNTRIES:
            if len(references) - found_in_germany >= abroad:
                break
            await exhaust_region(f"{country} (Land)")

        async def fill(params: dict[str, Any], count: int, target: int) -> None:
            for page in range(1, _page_count(count) + 1):
                if len(references) >= target:
                    return
                payload = await self._fetch_page(
                    client,
                    sem,
                    params={**params, "size": PAGE_SIZE, "page": page},
                )
                items = _result_items(payload)
                if not items:
                    return
                await record(items)

        for sort in _COVER_SORTS:
            await fill({**base_params, "sort": sort}, total, total)

        accepted = total - int(total * REGION_COVER_TOLERANCE)
        facets = initial_payload.get("facetten")
        for facet_name in _COVER_FACETS:
            if facet_name in base_params:
                continue
            for value, count in sorted(
                _bucket_counts(facets, facet_name).items(),
                key=lambda item: item[1],
                reverse=True,
            ):
                if len(references) >= accepted:
                    break
                if count <= PAGINATION_CAP:
                    await fill({**base_params, facet_name: value}, count, accepted)

        end = await self._fetch_page(
            client,
            sem,
            params={**base_params, "size": 1, "page": 1},
        )
        end_total = _result_total(end)
        if abs(end_total - total) > total - accepted:
            raise ScraperError(
                f"Bundesagentur slice changed from {total} to {end_total} "
                f"jobs during its region cover for params={base_params}"
            )
        if len(references) < accepted:
            raise ScraperError(
                f"Bundesagentur regions covered {len(references)} of "
                f"{total} jobs for params={base_params}"
            )

    async def _collect_verified_cover(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        """Recover an oversized leaf without treating overlaps as partitions.

        The API exposes several official sort orders and high-cardinality
        facets, each of which reveals a different slice before the 10k cap.
        Collect their union locally, deduplicate by reference number, and only
        release rows after two consecutive passes each recover their complete
        advertised count. The catalogue is live, so reference IDs may change
        between otherwise complete passes.
        """
        if total > MAX_COVER_TOTAL:
            raise ScraperError(
                f"Bundesagentur refuses an unbounded cover of {total} jobs "
                f"for params={base_params}"
            )

        consecutive_complete_passes = 0
        observations: list[str] = []
        for _attempt in range(MAX_COVER_ATTEMPTS):
            start = await self._fetch_page(
                client,
                sem,
                params={**base_params, "size": 1, "page": 1},
            )
            start_total = _result_total(start)
            if start_total > MAX_COVER_TOTAL:
                raise ScraperError(
                    f"Bundesagentur refuses an unbounded cover of "
                    f"{start_total} jobs for params={base_params}"
                )
            items_by_reference = await self._build_cover_pass(
                client,
                sem,
                base_params=base_params,
                total=start_total,
                initial_payload=start,
            )
            end = await self._fetch_page(
                client,
                sem,
                params={**base_params, "size": 1, "page": 1},
            )
            end_total = _result_total(end)
            references = set(items_by_reference)
            complete_pass = len(references) == start_total == end_total
            observations.append(
                f"{start_total}/{len(references)}/{end_total}"
            )
            if complete_pass:
                consecutive_complete_passes += 1
                if consecutive_complete_passes >= 2:
                    items = list(items_by_reference.values())
                    for offset in range(0, len(items), COVER_ABSORB_BATCH_SIZE):
                        await absorb(
                            items[offset:offset + COVER_ABSORB_BATCH_SIZE]
                        )
                    return
            else:
                consecutive_complete_passes = 0

        raise ScraperError(
            "Bundesagentur could not obtain two consecutive count-complete "
            f"covers for params={base_params} after {MAX_COVER_ATTEMPTS} "
            f"attempts (start/unique/end={observations})"
        )

    async def _build_cover_pass(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        initial_payload: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        items_by_reference: dict[str, dict[str, Any]] = {}

        async def collect_pages(params: dict[str, Any], count: int) -> bool:
            for page in range(1, _page_count(count) + 1):
                payload = await self._fetch_page(
                    client,
                    sem,
                    params={**params, "size": PAGE_SIZE, "page": page},
                )
                items = _result_items(payload)
                if not items:
                    return False
                for item in items:
                    reference = _item_reference(item)
                    if reference:
                        items_by_reference[reference] = item
                if len(items_by_reference) >= total:
                    return True
            return False

        for sort in _COVER_SORTS:
            if await collect_pages({**base_params, "sort": sort}, total):
                return items_by_reference

        facets = initial_payload.get("facetten")
        if isinstance(facets, dict):
            for facet_name in _COVER_FACETS:
                if facet_name in base_params:
                    continue
                counts = _bucket_counts(facets, facet_name)
                for value, count in sorted(
                    counts.items(), key=lambda item: item[1], reverse=True
                ):
                    if count > PAGINATION_CAP:
                        continue
                    if await collect_pages(
                        {**base_params, facet_name: value},
                        count,
                    ):
                        return items_by_reference

        return items_by_reference

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        # Sequential page fan-out within a single leaf — the recursion
        # tree provides cross-leaf parallelism via the global semaphore.
        # Bursting 50+ page requests for one leaf was the WAF trigger we
        # saw at concurrency=3.
        # Paging one leaf can take minutes while postings open and close, so
        # each page's own total sets how far to read.
        page_count = _page_count(total)
        page = 1
        while page <= page_count:
            params = {**base_params, "size": PAGE_SIZE, "page": page}
            payload = await self._fetch_page(client, sem, params=params)
            items = _result_items(payload)
            if not items:
                return
            await absorb(items)
            page_count = _page_count(_result_total(payload))
            page += 1

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        invalid_payloads = 0
        transient_attempts = 0
        while True:
            r: httpx.Response | None = None
            network_error: httpx.HTTPError | None = None
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
                    network_error = exc
            if r is not None and r.status_code == 200:
                try:
                    return _checked_payload(r, params)
                except (ScraperError, ValueError) as exc:
                    invalid_payloads += 1
                    if invalid_payloads >= MAX_RETRIES:
                        raise ScraperError(
                            "Bundesagentur returned an invalid 200 payload for "
                            f"{params} after {MAX_RETRIES} retries: {exc}"
                        ) from exc
                await asyncio.sleep(_retry_delay(invalid_payloads))
                continue
            if r is not None and r.status_code == 400:
                raise ScraperError(
                    f"Bundesagentur rejected query {params}: {r.text[:120]}"
                )
            if r is not None and not _is_transient(r.status_code):
                # 401, 404 and other 4xx are contract breaks: crash rather
                # than publish an undercount.
                raise ScraperError(
                    f"Bundesagentur returned {r.status_code} for {params}: "
                    f"{r.text[:120]}"
                )
            transient_attempts += 1
            self._count_transient_failure(r, network_error, params)
            if transient_attempts >= MAX_BLOCKED_ATTEMPTS:
                # The pipeline removes its temporary CSV and preserves the
                # previous complete provider output.
                if r is None:
                    raise _PageFetchExhaustedError(
                        f"Bundesagentur exhausted retries for {params}: "
                        f"{network_error!r}"
                    ) from network_error
                raise _PageFetchExhaustedError(
                    f"Bundesagentur returned {r.status_code} for {params} "
                    f"after {transient_attempts} attempts"
                )
            retry_after = r.headers.get("Retry-After") if r is not None else None
            await asyncio.sleep(_retry_delay(transient_attempts, retry_after))

    def _count_transient_failure(
        self,
        response: httpx.Response | None,
        network_error: httpx.HTTPError | None,
        params: dict[str, Any],
    ) -> None:
        if response is not None:
            self.transient_failures[str(response.status_code)] += 1
            logger.debug(
                "Bundesagentur returned %d for %s (Via=%s, X-API-BLOCKED=%s, "
                "Retry-After=%s, body=%r)",
                response.status_code,
                params,
                response.headers.get("Via"),
                response.headers.get("X-API-BLOCKED"),
                response.headers.get("Retry-After"),
                response.text[:200],
            )
        else:
            self.transient_failures[type(network_error).__name__] += 1
            logger.debug("Bundesagentur request for %s failed: %r", params, network_error)
        total = self.transient_failures.total()
        if total > MAX_TRANSIENT_FAILURES:
            raise _PageFetchExhaustedError(
                f"Bundesagentur met {total} transient failures in one run "
                f"({dict(self.transient_failures)}), the last for {params}"
            )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = _item_reference(item)
        title = str(
            item.get("stellenangebotsTitel")
            or item.get("titel")
            or item.get("hauptberuf")
            or ""
        ).strip()
        if not ats_id or not title:
            return None
        location_items = _locations(item)
        location = _format_locations(location_items)
        country_codes = {
            country_iso
            for location_item in location_items
            if (country_iso := _country_iso(_location_address(location_item).get("land")))
        }
        country_iso = next(iter(country_codes)) if len(country_codes) == 1 else None
        coordinate_location = location_items[0] if len(location_items) == 1 else {}
        company = str(
            item.get("firma") or item.get("arbeitgeber") or "Bundesagentur"
        ).strip() or "Bundesagentur"

        # Each posting has a deterministic public URL on jobsuche.arbeitsagentur.de.
        # The detail endpoint expects base64(refnr); the human URL accepts refnr.
        url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ats_id}"

        commitment, employment_type = _employment_details(item)
        remote_value = item.get("homeofficemoeglich")
        is_remote = remote_value if isinstance(remote_value, bool) else None

        department = _text(item.get("berufsfeld"))
        team = _text(item.get("branche"))
        if team == department:
            team = None

        salary_min = _number(item.get("gehaltsspanneVon"))
        salary_max = _number(item.get("gehaltsspanneBis"))
        salary_period = _salary_period(item.get("verguetungsangabe"))

        raw: dict[str, Any] = {}
        for k in (
            "aenderungsdatum",
            "alleBerufe",
            "arbeitgeberKundennummerHash",
            "arbeitszeitSchichtNachtWochenende",
            "arbeitszeitTeilzeitAbend",
            "arbeitszeitTeilzeitFlexibel",
            "arbeitszeitTeilzeitNachmittag",
            "arbeitszeitTeilzeitVormittag",
            "arbeitszeitVollzeit",
            "berufsfeld",
            "branche",
            "chiffrenummer",
            "hauptberuf",
            *_CONTRACT_FIELDS,
            "istGeringfuegigeBeschaeftigung",
            "quereinstiegGeeignet",
            "stellenlokationen",
        ):
            v = item.get(k)
            if v not in (None, "", [], {}):
                raw[k] = v

        externe_url = item.get("externeURL") or item.get("externeUrl")
        apply_url = externe_url if isinstance(externe_url, str) and externe_url.startswith("http") else None

        description = _text(item.get("stellenangebotsBeschreibung"))

        return Job(
            url=HttpUrl(url),
            title=title,
            company=company,
            ats_type=ATSType.BUNDESAGENTUR,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Europe" if country_iso else None,
            lat=_number(coordinate_location.get("breite")),
            lon=_number(coordinate_location.get("laenge")),
            is_remote=is_remote,
            salary_currency="EUR" if salary_min is not None or salary_max is not None else None,
            salary_period=salary_period,
            salary_min=salary_min,
            salary_max=salary_max,
            department=department,
            team=team,
            employment_type=employment_type,
            engagement_type=_engagement_type(item),
            commitment=commitment,
            apply_url=HttpUrl(apply_url) if apply_url else None,
            requisition_id=_text(item.get("chiffrenummer") or item.get("hashId")),
            description=description[:25_000] if description else None,
            posted_at=_parse_iso(
                item.get("datumErsteVeroeffentlichung")
                or _nested_value(item.get("veroeffentlichungszeitraum"), "von")
                or item.get("aktuelleVeroeffentlichungsdatum")
            ),
            fetched_at=datetime.now(UTC),
            language="de",
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
_ARBEITSZEIT_TO_EMPLOYMENT_TYPE: dict[str, EmploymentType] = {
    "vz": "FULL_TIME",
    "tz": "PART_TIME",
    "ho": "FULL_TIME",
    "mj": "PART_TIME",
    "ne": "PART_TIME",
    "saison": "TEMPORARY",
    "selb": "CONTRACT",
}

_COUNTRY_ISO = {
    "DEUTSCHLAND": "DE",
    "OESTERREICH": "AT",
    "ÖSTERREICH": "AT",
    "SCHWEIZ": "CH",
    "FRANKREICH": "FR",
    "NIEDERLANDE": "NL",
    "BELGIEN": "BE",
    "LUXEMBURG": "LU",
    "POLEN": "PL",
    "TSCHECHIEN": "CZ",
    "DÄNEMARK": "DK",
    "DAENEMARK": "DK",
}

_SALARY_PERIODS: dict[str, SalaryPeriod] = {
    "STUNDENLOHN": "HOUR",
    "TAGESENTGELT": "DAY",
    "WOCHENENTGELT": "WEEK",
    "MONATSENTGELT": "MONTH",
    "MONATSGEHALT": "MONTH",
    "JAHRESGEHALT": "YEAR",
}


def _checked_payload(
    response: httpx.Response, params: dict[str, Any]
) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ScraperError("Bundesagentur returned a non-object payload")
    total = _result_total(payload)
    page = int(params.get("page", 1))
    start = (page - 1) * int(params.get("size", PAGE_SIZE))
    # The API omits the list for a region with no postings and for a page
    # that starts past the end of the results. Elsewhere results are
    # expected, so that payload is retried.
    if "ergebnisliste" not in payload and (
        (page > 1 and start >= total) or ("wo" in params and total == 0)
    ):
        payload["ergebnisliste"] = []
    # Paging stops at the first empty page, so only the end may be empty.
    if not _result_items(payload) and start < total:
        raise ScraperError(
            f"Bundesagentur returned no jobs from position {start} of {total}"
        )
    return payload


def _page_count(total: int) -> int:
    return min((total + PAGE_SIZE - 1) // PAGE_SIZE, PAGE_LIMIT)


def _is_transient(status: int) -> bool:
    return status in (403, 429) or 500 <= status < 600


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Backoff before retry ``attempt + 1``, jittered so concurrent retries
    don't fall back into step, and at most RETRY_MAX_DELAY, a Retry-After
    included."""
    if retry_after and retry_after.isdigit():
        base = float(retry_after)
    else:
        base = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    jittered = base * (1 + random.uniform(-RETRY_JITTER, RETRY_JITTER))
    return min(jittered, RETRY_MAX_DELAY)


def _result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("ergebnisliste")
    if not isinstance(items, list):
        raise ScraperError("Bundesagentur v6 response is missing ergebnisliste")
    if not all(isinstance(item, dict) for item in items):
        raise ScraperError("Bundesagentur v6 returned a non-object job")
    return items


def _result_total(payload: dict[str, Any]) -> int:
    value = payload.get("maxErgebnisse")
    if isinstance(value, bool):
        raise ScraperError("Bundesagentur response has invalid maxErgebnisse")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScraperError(
            "Bundesagentur response is missing a valid maxErgebnisse"
        ) from exc
    if total < 0:
        raise ScraperError("Bundesagentur response has negative maxErgebnisse")
    return total


def _item_reference(item: dict[str, Any]) -> str:
    return str(item.get("referenznummer") or item.get("refnr") or "").strip()


_CONTRACT_FIELDS = (
    "stellenangebotsart", "ausbildungsart", "vertragsdauer", "befristungInMonaten",
    "istArbeitnehmerUeberlassung", "arbeitszeit", "zeitarbeit", "befristung",
)


def _engagement_type(item: dict[str, Any]) -> EngagementType | None:
    kind = item.get("stellenangebotsart")
    if kind == "SELBSTAENDIGKEIT" or item.get("arbeitszeit") == "selb":
        return "independent_contractor"
    if kind == "AUSBILDUNG" and item.get("ausbildungsart") == "AUSBILDUNG":
        return "apprenticeship"
    # PRAKTIKUM_TRAINEE covers both internships and trainee jobs. Agency
    # placement also does not tell us whether the worker's contract expires.
    duration = item.get("vertragsdauer")
    if duration == "UNBEFRISTET":
        return "permanent"
    if duration == "BEFRISTET":
        return "fixed_term"
    return None


def _employment_details(
    item: dict[str, Any],
) -> tuple[str | None, EmploymentType | None]:
    legacy = _text(item.get("arbeitszeit"))
    if legacy:
        code = legacy.lower()
        commitment = _ARBEITSZEIT_LABELS.get(code, legacy)
        legacy_employment_type = _ARBEITSZEIT_TO_EMPLOYMENT_TYPE.get(code)
        if legacy_employment_type is None and item.get("zeitarbeit") is True:
            legacy_employment_type = "TEMPORARY"
        if (
            legacy_employment_type is None
            and str(item.get("befristung") or "") == "2"
        ):
            legacy_employment_type = "CONTRACT"
        return commitment, legacy_employment_type

    labels: list[str] = []
    is_full_time = item.get("arbeitszeitVollzeit") is True
    is_part_time = any(
        item.get(key) is True
        for key in (
            "arbeitszeitTeilzeitAbend",
            "arbeitszeitTeilzeitFlexibel",
            "arbeitszeitTeilzeitNachmittag",
            "arbeitszeitTeilzeitVormittag",
        )
    )
    is_minijob = item.get("istGeringfuegigeBeschaeftigung") is True
    if is_full_time:
        labels.append("Vollzeit")
    if is_part_time:
        labels.append("Teilzeit")
    if is_minijob:
        labels.append("Minijob")
    if item.get("arbeitszeitSchichtNachtWochenende") is True:
        labels.append("Schicht/Nacht/Wochenende")

    if item.get("istArbeitnehmerUeberlassung") is True:
        employment_type: EmploymentType | None = "TEMPORARY"
    elif is_full_time:
        employment_type = "FULL_TIME"
    elif is_part_time or is_minijob:
        employment_type = "PART_TIME"
    elif item.get("vertragsdauer") == "BEFRISTET":
        employment_type = "CONTRACT"
    else:
        employment_type = None
    return ", ".join(labels) or None, employment_type


def _locations(item: dict[str, Any]) -> list[dict[str, Any]]:
    locations = item.get("stellenlokationen")
    if isinstance(locations, list):
        parsed = [value for value in locations if isinstance(value, dict)]
        if parsed:
            return parsed
    legacy = item.get("arbeitsort")
    return [{"adresse": legacy}] if isinstance(legacy, dict) else []


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


def _select_partition(
    payload: dict[str, Any],
    *,
    total: int,
    applied: set[str],
) -> tuple[str, dict[str, int]] | None:
    facets = payload.get("facetten")
    if not isinstance(facets, dict):
        return None

    candidates: list[tuple[int, int, str, dict[str, int]]] = []
    for priority, facet_name in enumerate(_SUBDIVISION_FACETS):
        if facet_name in applied:
            continue
        counts = _bucket_counts(facets, facet_name)
        if len(counts) < 2 or sum(counts.values()) != total:
            continue
        largest_bucket = max(counts.values())
        if largest_bucket >= total:
            continue
        candidates.append((largest_bucket, priority, facet_name, counts))

    if not candidates:
        return None
    _, _, facet_name, counts = min(candidates)
    return facet_name, counts


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    address = value.get("adresse")
    if isinstance(address, dict):
        value = address

    postal_code = _text(value.get("plz"))
    city = _text(value.get("ort"))
    locality = " ".join(part for part in (postal_code, city) if part) or None
    parts = [
        locality,
        _region_name(value.get("region")),
        _display_enum(value.get("land")),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part)) or None


def _format_locations(values: list[dict[str, Any]]) -> str | None:
    locations = [
        location
        for value in values
        if (location := _format_location(value)) is not None
    ]
    return " | ".join(dict.fromkeys(locations)) or None


def _location_address(value: dict[str, Any]) -> dict[str, Any]:
    address = value.get("adresse")
    return address if isinstance(address, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_enum(value: object) -> str | None:
    raw = _text(value)
    return raw.replace("_", " ").title() if raw else None


def _region_name(value: object) -> str | None:
    return _display_enum(value)


def _country_iso(value: object) -> str | None:
    raw = _text(value)
    return _COUNTRY_ISO.get(raw.upper()) if raw else None


def _salary_period(value: object) -> SalaryPeriod | None:
    raw = _text(value)
    return _SALARY_PERIODS.get(raw.upper()) if raw else None


def _nested_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
