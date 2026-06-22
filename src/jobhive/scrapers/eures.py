"""EURES (European Employment Services) scraper.

EURES aggregates job vacancies across the 31 EU/EEA countries. The
public portal at ``europa.eu/eures`` exposes an unauthenticated JSON
API the frontend consumes:

    POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search

The API caps every query at **10,000 results** (50 ``resultsPerPage`` ×
200 ``page`` max). Past page=200 the server returns 400. To collect the
full ~2.7M jobs we subdivide recursively, in priority order:

1. ``locationCodes`` — country code (de, fr, it, …). 31 buckets.
2. NUTS regions inside the country (de1..de7) — read from the response's
   ``POSITION_LOCATION`` facet ``childrenList``. Used when a country
   alone exceeds the 10k cap.
3. ``sectorCodes`` (NACE A..U) — 21 buckets. Used when a region still
   exceeds the cap.
4. ``positionScheduleCodes`` (fulltime/parttime/flextime/etc.) — final
   fallback.

Each response carries a ``facets`` block with per-bucket counts so we
plan subdivision optimally without extra probes (same trick as the
Bundesagentur scraper).

Single-source scraper: ``company_slug`` is informational and ignored.
The output rows carry the publishing employer's name as ``company`` so
the publisher's cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any

log = logging.getLogger(__name__)

API_URL = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
)
DETAIL_URL_FMT = (
    "https://europa.eu/eures/portal/jv-se/jv-details/{jv_id}?lang=en"
)
DETAIL_API_URL_FMT = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv/id/{jv_id}?lang=en"
)
PAGE_SIZE = 50  # API caps `resultsPerPage` at 50 (>50 returns 400).
PAGE_LIMIT = 200  # `page` caps at 200 (page>200 returns 400).
PAGINATION_CAP = PAGE_SIZE * PAGE_LIMIT  # 10,000 jobs per query.
MAX_CONCURRENCY = 6  # The portal is generous but we stay polite.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
MAX_SUBDIVISION_DEPTH = 4

# 31 EURES countries (EU 27 + EEA 3 + Switzerland), as used by the
# ``locationCodes`` filter. Codes match ISO 3166-1 alpha-2 lowercased.
_COUNTRIES = (
    "at", "be", "bg", "ch", "cy", "cz", "de", "dk", "ee", "el",
    "es", "fi", "fr", "hr", "hu", "ie", "is", "it", "li", "lt",
    "lu", "lv", "mt", "nl", "no", "pl", "pt", "ro", "se", "si",
    "sk",
)

# NACE sectors A..U.
_NACE_SECTORS = tuple("abcdefghijklmnopqrstu")

# Many EURES rows ship with a placeholder employer — confirmed
# 86% of FR rows ("non renseigné") and 60% of ES rows ("") in a
# May 2026 dump. These are real jobs (titles, descriptions and
# locations are all meaningful) but the employer is hidden by the
# source NES (France Travail, SEPE, …) for privacy reasons and is
# only revealed once a candidate applies via the official portal.
#
# Earlier versions dropped these rows entirely — costing the 1.7 M
# FR+ES catalog the user asked us to keep. We now pass the source
# value through verbatim (including the localized placeholder
# string or empty value): the locale of the placeholder is itself
# useful signal about the source NES, and downstream consumers
# can decide how to render it without us hard-coding a canonical
# English marker on their behalf.

# Position schedule values from the API enum.
_SCHEDULES = ("fulltime", "parttime", "flextime", "NS")


def _empty_search_body(rpp: int = PAGE_SIZE, page: int = 1) -> dict[str, Any]:
    """Skeleton search body. Extra keys override the empty defaults."""
    return {
        "resultsPerPage": rpp,
        "page": page,
        "sortSearch": "MOST_RECENT",
        "keywords": [],
        "publicationPeriod": None,
        "occupationUris": [],
        "skillUris": [],
        "requiredExperienceCodes": [],
        "positionScheduleCodes": [],
        "sectorCodes": [],
        "educationAndQualificationLevelCodes": [],
        "positionOfferingCodes": [],
        "locationCodes": [],
        "euresFlagCodes": [],
        "otherBenefitsCodes": [],
        "requiredLanguages": [],
        "minNumberPost": None,
        "sessionId": "jobhive",
        "requestLanguage": "en",
    }


@ScraperRegistry.register(ATSType.EURES)
class EuresScraper(BaseScraper):
    """EURES (EU public employment services) jobs API. Single-source —
    ``company_slug`` is ignored."""

    ats = ATSType.EURES

    def fetch(self) -> list[Job]:
        """Legacy in-memory fetch — accumulates the full corpus into a
        list. At ~2.7 M jobs that's ~10 GB RSS which exceeds our 7.6 GB
        VPS, so prefer :meth:`fetch_stream` from cron contexts that
        write straight to disk."""
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            cleaned = _clean_description_text(job.description)
            if cleaned:
                return cleaned
        if not job.ats_id:
            return _job_summary_description(job)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    DETAIL_API_URL_FMT.format(jv_id=job.ats_id),
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
            if response.status_code != 200:
                return _job_summary_description(job)
            return _extract_detail_description(response.json()) or _job_summary_description(job)
        except (httpx.HTTPError, ValueError):
            return _job_summary_description(job)

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Memory profile: ~200 MB regardless of corpus size — only the
        ``seen`` ID set + a bounded in-flight queue stays resident.
        The producer-side fan-out and dedup logic is shared with the
        legacy :meth:`fetch` via :meth:`_fetch_async`; we just plug a
        queue-pushing ``on_job`` callback into it and yield from the
        queue on the consumer side.

        Termination uses an ``asyncio.Event`` rather than a queue
        sentinel: the consumer polls ``queue.get`` with a 500 ms
        timeout and checks ``producer_done`` between polls. This
        avoids the deadlock that a bounded-queue sentinel-put would
        introduce if the consumer ever stops draining (cubic PR #69
        P1) and means producer cleanup is always non-blocking.

        Usage from :func:`scripts.run_pipeline.run` (for ATSes whose
        full output would exceed RAM):

        .. code-block:: python

            scraper = EuresScraper("eures", timeout=30)
            async for job in scraper.fetch_stream():
                writer.writerow(_job_to_row(job))
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
                    # Propagate any producer exception.
                    await task
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue  # re-check producer_done on next iteration
                yield item
        except BaseException:
            task.cancel()
            raise

    async def _fetch_async(
        self,
        *,
        on_job: Callable[[Job], Awaitable[None]] | None = None,
    ) -> list[Job]:
        """Drive the per-country fan-out + dedup.

        Two modes:

        - ``on_job is None`` (default): accumulate every deduped job
          into a list and return it. Used by :meth:`fetch` for small-
          corpus / test paths.

        - ``on_job`` set to an async callback: dispatch each deduped
          job to the callback instead of accumulating. Used by
          :meth:`fetch_stream` so the queue consumer can write jobs
          to disk as they land; the in-memory footprint drops to just
          the ``seen`` ID set (~100 MB at full corpus). Returns an
          empty list in this mode.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []
        lock = asyncio.Lock()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

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

            # Country-level fan-out — even tiny markets get their own
            # query so the 10k cap is split before we have to look at
            # facets at all.
            async def per_country(cc: str) -> None:
                await self._exhaust_query(
                    client, sem,
                    base={"locationCodes": [cc]},
                    depth=0, used_dims=set(),
                    absorb=absorb,
                )
            await _gather_tolerant(
                (per_country(c) for c in _COUNTRIES),
                label="country",
            )
        return all_jobs

    async def _exhaust_query(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        depth: int,
        used_dims: set[str],
        absorb,
    ) -> None:
        """Pull every job matching ``base``. If the total exceeds the
        per-query cap, pick the next subdivision dimension and recurse."""
        first = await self._search(client, sem, base=base, page=1)
        total = int(first.get("numberRecords") or 0)
        if total == 0:
            return
        await absorb(first.get("jvs") or [])

        if total <= PAGINATION_CAP:
            await self._fan_out_pages(
                client, sem, base=base, total=total, absorb=absorb,
            )
            return

        if depth >= MAX_SUBDIVISION_DEPTH:
            # Out of depth — accept the cap loss.
            await self._fan_out_pages(
                client, sem, base=base, total=PAGINATION_CAP, absorb=absorb,
            )
            return

        # Pick the next subdivision dimension we haven't applied yet.
        # Order: NUTS region under the active country → NACE sector →
        # schedule. Region first because for a single country it splits
        # most cleanly (regions are named NUTS-1 / NUTS-2 codes).
        if "region" not in used_dims and base.get("locationCodes"):
            children = _region_children_for(
                first.get("facets") or {}, base["locationCodes"],
            )
            if children:
                async def child_region(code: str) -> None:
                    await self._exhaust_query(
                        client, sem,
                        base={**base, "locationCodes": [code]},
                        depth=depth + 1,
                        used_dims=used_dims | {"region"},
                        absorb=absorb,
                    )
                await _gather_tolerant(
                    (child_region(c) for c in children),
                    label="region",
                )
                return

        if "sector" not in used_dims:
            facet = (first.get("facets") or {}).get("NACE_CODE") or {}
            sectors = [
                e["code"] for e in (facet.get("facetEntriesList") or [])
                if (e.get("count") or 0) > 0
            ] or list(_NACE_SECTORS)
            async def child_sector(code: str) -> None:
                await self._exhaust_query(
                    client, sem,
                    base={**base, "sectorCodes": [code]},
                    depth=depth + 1,
                    used_dims=used_dims | {"sector"},
                    absorb=absorb,
                )
            await _gather_tolerant(
                (child_sector(c) for c in sectors),
                label="sector",
            )
            return

        if "schedule" not in used_dims:
            async def child_sched(code: str) -> None:
                await self._exhaust_query(
                    client, sem,
                    base={**base, "positionScheduleCodes": [code]},
                    depth=depth + 1,
                    used_dims=used_dims | {"schedule"},
                    absorb=absorb,
                )
            await _gather_tolerant(
                (child_sched(c) for c in _SCHEDULES),
                label="schedule",
            )
            return

        # Exhausted dimensions — accept the cap loss for this slice.
        await self._fan_out_pages(
            client, sem, base=base, total=PAGINATION_CAP, absorb=absorb,
        )

    async def _fan_out_pages(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        total: int,
        absorb,
    ) -> None:
        # Page 1 is already absorbed by the caller.
        page_count = min((total + PAGE_SIZE - 1) // PAGE_SIZE, PAGE_LIMIT)
        if page_count <= 1:
            return

        async def one(page: int) -> None:
            payload = await self._search(client, sem, base=base, page=page)
            await absorb(payload.get("jvs") or [])

        await _gather_tolerant(
            (one(p) for p in range(2, page_count + 1)),
            label="page",
        )

    async def _search(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        page: int,
    ) -> dict[str, Any]:
        body = _empty_search_body(PAGE_SIZE, page)
        body.update(base)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    r = await client.post(
                        API_URL, json=body,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
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
                        f"EURES returned non-JSON for {base}: {exc}"
                    ) from exc
            if r.status_code == 400:
                # Past pagination cap or invalid filter — return empty
                # so the caller treats this slice as exhausted.
                return {"numberRecords": 0, "jvs": [], "facets": {}}
            # 307 with an HTML "Network Error" body is the
            # CDN/load-balancer in front of EURES timing out; the
            # next attempt routes through a fresh upstream and almost
            # always succeeds. Treat it the same as 429/5xx so we
            # exhaust ``MAX_RETRIES`` instead of giving up on the
            # very first redirect. Observed 2026-05-11: 5 711 page
            # failures were 307-with-error-page, costing ~285 k rows
            # of the EURES corpus when the previous code treated 307
            # as terminal.
            if (
                r.status_code in (307, 429)
                or 500 <= r.status_code < 600
            ):
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"EURES returned {r.status_code} after "
                        f"{MAX_RETRIES} retries for {base} page={page}"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"EURES returned {r.status_code} for {base} page={page}: "
                f"{r.text[:120]}"
            )
        raise ScraperError(
            f"EURES exhausted retries for {base} page={page}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        jv_id = item.get("id")
        title = (item.get("title") or "").strip()
        if not jv_id or not title:
            return None

        # Employer — sometimes nested in ``employerName``, sometimes a
        # flat string. The source NES often anonymizes the employer
        # for privacy reasons (FR uses "non renseigné" at ~86%,
        # ES uses an empty string at ~60%). Pass the source value
        # through verbatim — see the module-level comment for the
        # rationale around keeping the localized placeholder text
        # instead of canonicalizing it.
        employer = (
            item.get("employerName")
            or (item.get("employer") or {}).get("name")
            or ""
        ).strip()

        location = _flatten_location(item.get("locationMap") or {})
        posted_at = _epoch_ms_to_dt(item.get("creationDate"))

        # EURES ships a freeform-ish ``positionOfferingCode``
        # ("directhire", "temporary", "contract", "apprenticeship",
        # "seasonal", "oncall", "selfemployed", …) — map to the
        # canonical employment-type enum and surface the original
        # code as ``commitment`` for display.
        offering = item.get("positionOfferingCode")
        commitment: str | None = None
        employment_type: str | None = None
        if isinstance(offering, str) and offering.strip():
            commitment = offering.strip()
            norm = commitment.lower()
            employment_type = _OFFERING_CODE_TO_EMPLOYMENT_TYPE.get(norm)
            if not employment_type:
                for needle, mapped in _OFFERING_CODE_TO_EMPLOYMENT_TYPE.items():
                    if needle in norm:
                        employment_type = mapped
                        break

        # ``positionScheduleCode`` (full-time / part-time) — used as a
        # fallback when ``positionOfferingCode`` is missing/unspecific.
        schedule = item.get("positionScheduleCode")
        if isinstance(schedule, str) and schedule.strip() and not employment_type:
            sched_norm = schedule.strip().lower()
            if sched_norm in ("fulltime", "full-time", "full_time"):
                employment_type = "FULL_TIME"
            elif sched_norm in ("parttime", "part-time", "part_time"):
                employment_type = "PART_TIME"

        raw: dict[str, Any] = {}
        for k in ("euresFlag", "numberOfPosts", "lastModificationDate",
                  "positionOfferingCode", "positionScheduleCode"):
            v = item.get(k)
            if v not in (None, "", []):
                raw[k] = v

        return Job(
            url=DETAIL_URL_FMT.format(jv_id=jv_id),
            title=title,
            company=employer,
            ats_type=ATSType.EURES,
            ats_id=str(jv_id),
            location=location,
            employment_type=employment_type,
            commitment=commitment,
            description=_extract_description(item),
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# EURES ``positionOfferingCode`` is a stable enum across PES feeds.
_OFFERING_CODE_TO_EMPLOYMENT_TYPE = {
    "directhire": "FULL_TIME",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "contracttohire": "CONTRACT",
    "selfemployed": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "temporarytohire": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "oncall": "TEMPORARY",
    "casual": "TEMPORARY",
    "apprenticeship": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "traineeship": "INTERN",
}


def _epoch_ms_to_dt(value: object) -> datetime | None:
    """EURES dates are unix-epoch milliseconds. Convert to UTC datetime
    or return None on missing/garbage."""
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000)


def _flatten_location(loc_map: dict[str, Any]) -> str | None:
    """``locationMap`` is ``{"DE": ["DE12", "DE34"], ...}``. Render the
    first country's code(s) as a short string."""
    if not loc_map:
        return None
    country = next(iter(loc_map))
    regions = [r for r in (loc_map[country] or []) if isinstance(r, str)]
    if regions:
        return f"{country} ({', '.join(regions[:3])})"
    return country


def _extract_description(item: dict[str, Any]) -> str | None:
    value = item.get("description")
    if not isinstance(value, str) or not value.strip():
        translations = item.get("translations") or {}
        if isinstance(translations, dict):
            for translation in translations.values():
                if isinstance(translation, dict):
                    candidate = translation.get("description")
                    if isinstance(candidate, str) and candidate.strip():
                        value = candidate
                        break
    if not isinstance(value, str) or not value.strip():
        return None
    return _clean_description_text(value)


def _extract_detail_description(payload: dict[str, Any]) -> str | None:
    """Extract the richest text available from the EURES detail API.

    Some national feeds publish a listing with no ``description`` at all,
    but the detail API still has application instructions, employer text,
    or required skills. Use those as a last-resort description so the row
    remains searchable without hitting the browser-rendered Angular page.
    """
    candidates: list[str] = []
    translation = payload.get("translation")
    if isinstance(translation, dict):
        description = translation.get("description")
        if isinstance(description, str):
            candidates.append(description)

    profiles = payload.get("jvProfiles") or {}
    if isinstance(profiles, dict):
        preferred = payload.get("preferredLanguage")
        ordered_profiles = []
        if isinstance(preferred, str) and preferred in profiles:
            ordered_profiles.append(profiles[preferred])
        ordered_profiles.extend(
            profile
            for lang, profile in profiles.items()
            if lang != preferred
        )
        for profile in ordered_profiles:
            if not isinstance(profile, dict):
                continue
            description = profile.get("description")
            if isinstance(description, str):
                candidates.append(description)
            employer = profile.get("employer") or {}
            if isinstance(employer, dict):
                employer_description = employer.get("description")
                if isinstance(employer_description, str):
                    candidates.append(employer_description)
            skills = _detail_skills_text(profile.get("requiredSkills") or [])
            if skills:
                candidates.append(skills)
            instructions = _detail_instructions_text(
                profile.get("applicationInstructions") or []
            )
            if instructions:
                candidates.append(f"Application instructions: {instructions}")

    for candidate in candidates:
        cleaned = _clean_description_text(candidate)
        if cleaned:
            return cleaned
    return None


def _detail_instructions_text(values: object) -> str | None:
    if not isinstance(values, list):
        return None
    text = " ".join(str(value) for value in values if value)
    return text.strip() or None


def _detail_skills_text(values: object) -> str | None:
    if not isinstance(values, list):
        return None
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key in ("prefLabel", "label", "description", "name"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    parts.append(candidate)
                    break
    if not parts:
        return None
    return "Required skills: " + "; ".join(parts)


def _job_summary_description(job: Job) -> str | None:
    """Last-resort searchable text when EURES publishes no description.

    A small number of national feeds return an empty listing description
    and either an empty/404 detail response. Keep those rows non-empty by
    composing a factual summary from fields already present in the job.
    """
    parts = [job.title.strip()] if job.title and job.title.strip() else []
    if job.company and job.company.strip():
        parts.append(f"Employer: {job.company.strip()}")
    if job.location and job.location.strip():
        parts.append(f"Location: {job.location.strip()}")
    if job.employment_type and job.employment_type.strip():
        parts.append(f"Employment type: {job.employment_type.strip()}")
    if job.commitment and job.commitment.strip():
        parts.append(f"Contract type: {job.commitment.strip()}")
    return ". ".join(parts)[:25_000] or None


def _clean_description_text(value: str) -> str | None:
    text = html.unescape(value)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()[:25_000]
    if len(text) < 4:
        return None
    return text


async def _gather_tolerant(
    coros: Any,
    *,
    label: str,
) -> None:
    """Run every coroutine concurrently, log + swallow failures instead
    of cancelling siblings.

    The default ``asyncio.gather`` re-raises the first exception, which
    cancels every other pending task — one transient network blip in a
    deep recursion (300+ sub-queries per country) used to abort the
    whole scrape and leave the CSV at ~12 k of the ~1 M corpus
    (observed on the 2026-05-11 cron). With this helper, a failed
    sibling logs a warning and the rest of the tree keeps writing.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            log.warning("EURES %s subtask failed: %s", label, r)


def _region_children_for(
    facets: dict[str, Any],
    selected: list[str],
) -> list[str]:
    """Find regional children of the selected country in
    ``POSITION_LOCATION``. Returns a list of NUTS codes that we can
    pass back as ``locationCodes`` to subdivide."""
    if not selected:
        return []
    target = selected[0].lower()
    pos = (facets or {}).get("POSITION_LOCATION") or {}
    for entry in pos.get("facetEntriesList") or []:
        if (entry.get("code") or "").lower() != target:
            continue
        children = entry.get("childrenList") or []
        codes = [
            c.get("code") for c in children
            if c.get("code") and (c.get("count") or 0) > 0
        ]
        return codes
    return []
