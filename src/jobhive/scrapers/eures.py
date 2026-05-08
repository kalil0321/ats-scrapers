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
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
)
DETAIL_URL_FMT = (
    "https://europa.eu/eures/portal/jv-se/jv-details/{jv_id}?lang=en"
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

# Many EURES rows ship with a placeholder employer ("non renseigné",
# "EURES" etc.) — confirmed at ~33% of all rows in a May 2026 dump,
# 97% in NL. They're real jobs but useless without an identifiable
# employer; downstream dedup and any per-company analytics get
# polluted. Drop at parse time so the scraper output stays clean.
_PLACEHOLDER_COMPANIES: frozenset[str] = frozenset({
    "non renseigné", "non renseigne", "non rensigne",
    "eures",
    "siehe beschreibung", "see description",
    "no se especifica", "no especificado",
    "n/a", "na", "n.a.", "-", "—", "unspecified", "unknown",
    "company name not provided", "anonymous",
    "anoniem", "konfidentiell", "confidentiel", "confidential",
})

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
            await asyncio.gather(*(per_country(c) for c in _COUNTRIES))
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
                await asyncio.gather(*(child_region(c) for c in children))
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
            await asyncio.gather(*(child_sector(c) for c in sectors))
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
            await asyncio.gather(*(child_sched(c) for c in _SCHEDULES))
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

        await asyncio.gather(*(one(p) for p in range(2, page_count + 1)))

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
            if r.status_code == 429 or 500 <= r.status_code < 600:
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
        # flat string. Drop the row entirely if the source PES never
        # filled in the employer; the placeholder fallbacks
        # ("EURES", "non renseigné") are noise that pollutes downstream
        # dedup and analytics.
        employer = (
            item.get("employerName")
            or (item.get("employer") or {}).get("name")
            or ""
        ).strip()
        if not employer or employer.lower() in _PLACEHOLDER_COMPANIES:
            return None

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
