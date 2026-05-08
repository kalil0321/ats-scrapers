"""Arbetsförmedlingen (Swedish Public Employment Service) scraper.

Sweden's federal job board exposes a clean public JSON search API at
``jobsearch.api.jobtechdev.se`` — no auth, no API key. Every active
listing lives under one of 21 Swedish ``region`` codes; total volume is
~46k active jobs.

Pagination caps at ``offset+limit ≤ 10,000`` per query. Stockholm
(largest region, ~11k) is the only one that pushes past the cap; we
subdivide it by ``occupation-field`` to recover the trailing jobs.
Every other region fits in a single paginated stream.

Public API docs: https://jobtechdev.se/sv/komponenter/jobsearch
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

API_URL = "https://jobsearch.api.jobtechdev.se/search"
PAGE_SIZE = 100  # API hard-caps at 100/page.
PAGINATION_CAP = 10_000  # offset+limit cap. Past 10k the API returns 0 hits.
MAX_CONCURRENCY = 8
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Swedish region concept_ids (län). Static — the Arbetsförmedlingen
# taxonomy doesn't churn. Fetched from the API's ``stats=region`` facet.
SWEDEN_REGIONS = (
    "CifL_Rzy_Mku",  # Stockholms län
    "zdoY_6u5_Krt",  # Västra Götalands län
    "CaRE_1nn_cSU",  # Skåne län
    "oLT3_Q9p_3nn",  # Östergötlands län
    "MtbE_xWT_eMi",  # Jönköpings län
    "g5Tt_CXo_GTH",  # Uppsala län
    "9hXe_F4g_eTG",  # Hallands län
    "EVe9_z5Q_DJv",  # Södermanlands län
    "Pnmw_Tbx_2Eg",  # Örebro län
    "yiyJ_KFi_LX9",  # Gävleborgs län
    "JkJv_Ssr_2hG",  # Värmlands län
    "qXSE_AC1_RkA",  # Dalarnas län
    "tF3y_MF9_h5G",  # Norrbottens län
    "9TLG_xj1_VKA",  # Västerbottens län
    "K8iD_VQv_2BB",  # Västernorrlands län
    "DQZd_uYs_oKb",  # Kronobergs län
    "8QQ6_e95_R2P",  # Blekinge län
    "EFLm_8iL_4Wy",  # Gotlands län
    "NvUF_SP1_1zo",  # Kalmar län
    "txzq_TmJ_jUn",  # Jämtlands län
    "9YR1_AsT_eSc",  # Västmanlands län
)


@ScraperRegistry.register(ATSType.ARBETSFORMEDLINGEN)
class ArbetsformedlingenScraper(BaseScraper):
    """Sweden federal job board — single-source. ``company_slug`` ignored."""

    ats = ATSType.ARBETSFORMEDLINGEN

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []

        def absorb(items: list[dict[str, Any]]) -> None:
            for it in items:
                job = self._parse(it)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                all_jobs.append(job)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            # Fetch the live region list rather than hardcoding —
            # the taxonomy adjusts over time (län merges, retired codes,
            # etc.). ``stats.limit=30`` returns all 21 current regions
            # (default is 5, which is the trap that bit us before).
            seed = await self._fetch_page(
                client, sem,
                params={"limit": 0, "stats": "region", "stats.limit": 30},
            )
            stats = seed.get("stats") or []
            regions: list[str] = []
            if stats and isinstance(stats[0], dict):
                regions = [
                    v.get("concept_id")
                    for v in stats[0].get("values") or []
                    if isinstance(v, dict) and v.get("concept_id")
                ]
            if not regions:
                # Last-resort fallback to the static list — better to get
                # partial coverage than to crash if the API moves.
                regions = list(SWEDEN_REGIONS)
            await asyncio.gather(*(
                self._exhaust_region(client, sem, region, absorb)
                for region in regions
            ))
        return all_jobs

    async def _exhaust_region(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        region: str,
        absorb,
    ) -> None:
        first = await self._fetch_page(client, sem, params={
            "region": region, "limit": PAGE_SIZE, "offset": 0,
        })
        total = (first.get("total") or {}).get("value", 0)
        if total == 0:
            return
        absorb(first.get("hits") or [])
        if total <= PAGE_SIZE:
            return

        # Pagination cap — the trailing jobs past offset 10k are
        # unreachable without a finer filter. For Stockholm we subdivide
        # by occupation-field (currently ~28 buckets in 2026, each well
        # under 10k); other regions stay under the cap.
        if total > PAGINATION_CAP:
            await self._subdivide_by_occupation(client, sem, region, absorb)
            return

        offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
        await asyncio.gather(*(
            self._fetch_and_absorb(
                client, sem,
                params={"region": region, "limit": PAGE_SIZE, "offset": o},
                absorb=absorb,
            )
            for o in offsets
        ))

    async def _subdivide_by_occupation(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        region: str,
        absorb,
    ) -> None:
        # Discover occupation-field codes via stats — the API returns the
        # top buckets dynamically, which is fine because the largest
        # buckets are what we need to hit before the trailing cap.
        stats = await self._fetch_page(client, sem, params={
            "region": region, "limit": 0, "stats": "occupation-field",
        })
        fields = stats.get("stats") or []
        codes: list[str] = []
        if fields and isinstance(fields[0], dict):
            codes = [
                v.get("concept_id")
                for v in fields[0].get("values") or []
                if isinstance(v, dict) and v.get("concept_id")
            ]
        if not codes:
            # Fallback — just paginate up to the cap.
            offsets = list(range(PAGE_SIZE, PAGINATION_CAP, PAGE_SIZE))
            await asyncio.gather(*(
                self._fetch_and_absorb(
                    client, sem,
                    params={"region": region, "limit": PAGE_SIZE, "offset": o},
                    absorb=absorb,
                )
                for o in offsets
            ))
            return

        async def occ_bucket(code: str) -> None:
            sub = await self._fetch_page(client, sem, params={
                "region": region, "occupation-field": code,
                "limit": PAGE_SIZE, "offset": 0,
            })
            sub_total = min((sub.get("total") or {}).get("value", 0), PAGINATION_CAP)
            absorb(sub.get("hits") or [])
            if sub_total <= PAGE_SIZE:
                return
            offsets = list(range(PAGE_SIZE, sub_total, PAGE_SIZE))
            await asyncio.gather(*(
                self._fetch_and_absorb(
                    client, sem,
                    params={"region": region, "occupation-field": code,
                            "limit": PAGE_SIZE, "offset": o},
                    absorb=absorb,
                )
                for o in offsets
            ))

        await asyncio.gather(*(occ_bucket(c) for c in codes))

    async def _fetch_and_absorb(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, Any],
        absorb,
    ) -> None:
        payload = await self._fetch_page(client, sem, params=params)
        absorb(payload.get("hits") or [])

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
                        f"Arbetsförmedlingen returned non-JSON for {params}: {exc}"
                    ) from exc
            if r.status_code == 400:
                # Past pagination cap or invalid params — return empty.
                return {"hits": [], "total": {"value": 0}}
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Arbetsförmedlingen returned {r.status_code} after "
                        f"{MAX_RETRIES} retries for {params}"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Arbetsförmedlingen returned {r.status_code} for {params}"
            )
        raise ScraperError(
            f"Arbetsförmedlingen exhausted retries for {params}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("headline") or "").strip()
        url = item.get("webpage_url")
        if not ats_id or not title or not url:
            return None

        employer = item.get("employer") or {}
        company = (
            (employer.get("name") if isinstance(employer, dict) else None)
            or "Arbetsförmedlingen"
        )
        # ``workplace`` is the trading name / site label (e.g. parent
        # corp uses ``name``, the actual office is ``workplace``).
        team = (
            employer.get("workplace") if isinstance(employer, dict) else None
        )
        if isinstance(team, str) and team.strip().lower() == str(company).strip().lower():
            team = None  # Don't duplicate company name into team.

        wpl = item.get("workplace_address") or {}
        location = _format_location(wpl)

        description = None
        desc_obj = item.get("description")
        if isinstance(desc_obj, dict):
            description = (desc_obj.get("text") or "").strip() or None

        # Salary: rarely populated. ``salary_description`` is free-form
        # text; fall back to ``salary_type.label`` (e.g. "Fast månads-
        # vecko- eller timlön") so the column carries something useful.
        salary_summary = item.get("salary_description") or None
        if not salary_summary:
            sal_type = item.get("salary_type") or {}
            if isinstance(sal_type, dict):
                salary_summary = sal_type.get("label") or None

        # Working hours type: heltid (full-time) / deltid (part-time).
        working_hours_type = item.get("working_hours_type") or {}
        hours_label = (
            working_hours_type.get("label")
            if isinstance(working_hours_type, dict) else None
        )

        emp_type_obj = item.get("employment_type") or {}
        emp_label = emp_type_obj.get("label") if isinstance(emp_type_obj, dict) else None
        employment_type = _map_employment_type(emp_label, hours_label)

        # ``commitment`` is the schedule string. Prefer Heltid/Deltid;
        # fall back to scope_of_work range ("0–100 %") only when the
        # API didn't fill the working_hours_type. Don't leak the
        # employment-contract label here — that lives in ``employment_type``.
        commitment: str | None = (
            hours_label.strip() if isinstance(hours_label, str) and hours_label.strip()
            else None
        )
        if not commitment:
            scope = item.get("scope_of_work") or {}
            if isinstance(scope, dict):
                lo, hi = scope.get("min"), scope.get("max")
                if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > 0:
                    commitment = (
                        f"{int(hi)} %" if lo == hi
                        else f"{int(lo)}–{int(hi)} %"
                    )

        # ``occupation_field`` is the high-level domain (Pedagogik,
        # Hälso- och sjukvård, IT-data, etc.) — Arbetsförmedlingen's
        # closest analog to a department label.
        occ_field = item.get("occupation_field") or {}
        department = occ_field.get("label") if isinstance(occ_field, dict) else None

        # ``application_details.url`` is where to actually apply (often
        # external — employer site or LinkedIn).
        apply_details = item.get("application_details") or {}
        apply_url = (
            apply_details.get("url") if isinstance(apply_details, dict) else None
        )

        # Employer's own external ref — usually null but worth keeping
        # when present.
        requisition_id = (
            (item.get("external_id") or item.get("original_id") or "")
            .strip() or None
        ) if isinstance(item.get("external_id") or item.get("original_id"), str) else None

        is_remote = None
        if isinstance(item.get("remote_work"), bool):
            is_remote = item["remote_work"]

        raw: dict[str, Any] = {}
        for k in ("occupation", "occupation_field", "occupation_group",
                  "duration", "scope_of_work", "experience_required",
                  "salary_type", "must_have", "nice_to_have",
                  "employment_type", "working_hours_type"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=str(url),
            title=title,
            company=str(company).strip() or "Arbetsförmedlingen",
            ats_type=ATSType.ARBETSFORMEDLINGEN,
            ats_id=ats_id,
            location=location,
            is_remote=is_remote,
            team=team if isinstance(team, str) and team.strip() else None,
            description=description,
            department=department if isinstance(department, str) else None,
            employment_type=employment_type,
            commitment=commitment,
            apply_url=apply_url if isinstance(apply_url, str) and apply_url.startswith("http") else None,
            requisition_id=requisition_id,
            salary_summary=salary_summary,
            posted_at=_parse_iso(item.get("publication_date") or item.get("application_deadline")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# Swedish employment-type labels → our shared enum. The labels come from
# ``employment_type.label`` in the search response. ``working_hours_type``
# (heltid/deltid) refines the FT/PT split; everything else (internships,
# fixed-term contracts, on-call work) maps directly.
_EMP_TYPE_MAP = {
    "praktik": "INTERN",
    "ferieanställning": "INTERN",
    "sommarjobb": "TEMPORARY",
    "sommarjobb / feriejobb": "TEMPORARY",
    "säsongsarbete": "TEMPORARY",
    "säsongsanställning": "TEMPORARY",
    "behovsanställning": "TEMPORARY",
    "tidsbegränsad anställning": "CONTRACT",
    "tidsbegränsad": "CONTRACT",
    "vikariat": "CONTRACT",
    "projektanställning": "CONTRACT",
}


def _map_employment_type(emp_label: str | None, hours_label: str | None) -> str | None:
    """Coerce Swedish labels to our shared employment-type enum.

    Permanent positions ("Tillsvidareanställning" / "Vanlig anställning")
    don't carry a FT/PT signal on their own — those use ``hours_label``
    (Heltid → FULL_TIME, Deltid → PART_TIME). Anything else falls into
    one of the explicit mappings above.
    """
    if isinstance(emp_label, str) and emp_label.strip():
        key = emp_label.strip().lower()
        for needle, mapped in _EMP_TYPE_MAP.items():
            if needle in key:
                return mapped
    if isinstance(hours_label, str):
        h = hours_label.strip().lower()
        if "heltid" in h:
            return "FULL_TIME"
        if "deltid" in h:
            return "PART_TIME"
    if isinstance(emp_label, str) and emp_label.strip():
        # Permanent default — Tillsvidare / Vanlig anställning.
        return "FULL_TIME"
    return None


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        str(value[k]).strip()
        for k in ("municipality", "region", "country")
        if isinstance(value.get(k), str) and value.get(k).strip()
    ]
    return ", ".join(parts) or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
