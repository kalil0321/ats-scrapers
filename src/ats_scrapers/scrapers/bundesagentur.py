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
import base64
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
DETAIL_URL_TEMPLATE = (
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{encoded_ref}"
)
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


class _PageFetchExhaustedError(ScraperError):
    """Internal signal that ``_fetch_page`` exhausted its retry budget on
    a *transient* failure class (persistent 403 / 429 / 5xx, or a network
    error that didn't resolve before MAX_RETRIES).

    Distinguished from ``ScraperError`` so logs preserve the retry-exhausted
    failure class. It is deliberately not swallowed: publishing a partial
    federal catalogue is less safe than preserving the previous complete run.
    """


@ScraperRegistry.register(ATSType.BUNDESAGENTUR)
class BundesagenturScraper(BaseScraper):
    """Bundesagentur für Arbeit (DE) jobs API. Single-source scraper —
    ``company_slug`` is unused."""

    ats = ATSType.BUNDESAGENTUR

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(client, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Memory profile: ~200 MB regardless of corpus size — only the
        ``seen`` ID set + a bounded in-flight queue stays resident.
        Prefer this over the in-memory :meth:`fetch` / :meth:`afetch`
        from cron contexts that write straight to disk — at ~750 k jobs
        the accumulated list is a few GB of Job objects in RAM.
        Shares its fan-out + dedup logic with :meth:`afetch` by
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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            await self._exhaust_query(
                client, sem, base_params={}, depth=0, absorb=absorb,
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
            raise ScraperError(
                f"Bundesagentur cannot losslessly partition {total} jobs "
                f"for params={base_params}"
            )
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

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base_params: dict[str, Any],
        total: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
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
            await absorb(_result_items(payload))

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
                    payload = r.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Bundesagentur returned non-JSON for {params}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ScraperError(
                        f"Bundesagentur returned a non-object payload for {params}"
                    )
                return payload
            if r.status_code == 400:
                raise ScraperError(
                    f"Bundesagentur rejected query {params}: {r.text[:120]}"
                )
            # 403 here is a transient Akamai/WAF rate-limit, not a real
            # auth failure (the API key never expires); back off and retry.
            if r.status_code in (403, 429) or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    # Persistent WAF/server failure aborts the scrape. The
                    # pipeline removes its temporary CSV and preserves the
                    # previous complete provider output.
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
            # The scrape crashes loudly rather than producing an undercount.
            raise ScraperError(
                f"Bundesagentur returned {r.status_code} for {params}: "
                f"{r.text[:120]}"
            )
        # Network errors exhausted the retry budget; preserve the old output.
        raise _PageFetchExhaustedError(
            f"Bundesagentur exhausted retries for {params}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("referenznummer") or item.get("refnr") or "").strip()
        title = str(
            item.get("stellenangebotsTitel")
            or item.get("titel")
            or item.get("hauptberuf")
            or ""
        ).strip()
        if not ats_id or not title:
            return None
        location_data = _first_location(item)
        location = _format_location(location_data)
        address = location_data.get("adresse") if isinstance(location_data, dict) else None
        address = address if isinstance(address, dict) else {}
        company = str(
            item.get("firma") or item.get("arbeitgeber") or "Bundesagentur"
        ).strip() or "Bundesagentur"

        # Each posting has a deterministic public URL on jobsuche.arbeitsagentur.de.
        # The detail endpoint expects base64(refnr); the human URL accepts refnr.
        url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ats_id}"

        commitment, employment_type = _employment_details(item)
        remote_value = item.get("homeofficemoeglich")
        is_remote = remote_value if isinstance(remote_value, bool) else None

        department = _text(item.get("hauptberuf") or item.get("berufsfeld"))
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
            "branche",
            "chiffrenummer",
            "hauptberuf",
            "istArbeitnehmerUeberlassung",
            "istGeringfuegigeBeschaeftigung",
            "quereinstiegGeeignet",
            "stellenangebotsart",
            "vertragsdauer",
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
            country_iso=_country_iso(address.get("land")),
            region=_region_name(address.get("region")),
            lat=_number(location_data.get("breite")),
            lon=_number(location_data.get("laenge")),
            is_remote=is_remote,
            salary_currency="EUR" if salary_min is not None or salary_max is not None else None,
            salary_period=salary_period,
            salary_min=salary_min,
            salary_max=salary_max,
            department=department,
            team=team,
            employment_type=employment_type,
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


def _result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("ergebnisliste")
    if not isinstance(items, list):
        raise ScraperError("Bundesagentur v6 response is missing ergebnisliste")
    if not all(isinstance(item, dict) for item in items):
        raise ScraperError("Bundesagentur v6 returned a non-object job")
    return items


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


def _first_location(item: dict[str, Any]) -> dict[str, Any]:
    locations = item.get("stellenlokationen")
    if isinstance(locations, list) and locations and isinstance(locations[0], dict):
        return locations[0]
    legacy = item.get("arbeitsort")
    return {"adresse": legacy} if isinstance(legacy, dict) else {}


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
