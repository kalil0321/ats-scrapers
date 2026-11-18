"""Arbetsförmedlingen (Swedish Public Employment Service) scraper.

Sweden's federal job board exposes a clean public JSON search API at
``jobsearch.api.jobtechdev.se``: no auth, no API key. Total volume is
~40-50k active ads.

One query can read at most 2,100 ads: ``limit`` stops at 100 and any
``offset`` above 2,000 answers 400. The scraper therefore splits the
board into publication-date windows (``published-after`` and
``published-before``, both inclusive) and halves any window that still
matches more than 2,100 ads. Unlike region or occupation filters, every
ad has a publication date, so the windows cover the whole board.

Public API docs: https://jobtechdev.se/sv/komponenter/jobsearch
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from pydantic import ValidationError

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EducationRequirement, Job, WorkloadPercentages
from ats_scrapers.scrapers._job_classification import classification_label
from ats_scrapers.scrapers._skill_tags import skill_tags
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.models import SkillRequirement

log = logging.getLogger(__name__)

API_URL = "https://jobsearch.api.jobtechdev.se/search"
PAGE_SIZE = 100  # API hard-caps at 100/page.
MAX_OFFSET = 2_000  # Larger offsets answer 400.
WINDOW_CAP = MAX_OFFSET + PAGE_SIZE  # Most ads one query can return.
MAX_CONCURRENCY = 8
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
# A walk over today's board takes well under a hundred windows; a search
# that ignores the date filter would split until one-second windows.
MAX_DATE_WINDOWS = 1_000


@ScraperRegistry.register(ATSType.ARBETSFORMEDLINGEN)
class ArbetsformedlingenScraper(BaseScraper):
    """Sweden federal job board — single-source. ``company_slug`` ignored."""

    ats = ATSType.ARBETSFORMEDLINGEN

    default_headers: ClassVar[dict[str, str] | None] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    async def afetch(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []

        def absorb(items: list[dict[str, Any]]) -> None:
            for it in items:
                job = self._parse(it)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                all_jobs.append(job)

        async with self.make_fetcher() as fetch:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            newest, oldest = await asyncio.gather(
                self._fetch_page(fetch, sem, params={"limit": 1, "sort": "pubdate-desc"}),
                self._fetch_page(fetch, sem, params={"limit": 1, "sort": "pubdate-asc"}),
            )
            if not (newest.get("total") or {}).get("value"):
                return []
            end = _publication_date(newest)
            start = _publication_date(oldest)
            if start is None or end is None:
                raise ScraperError(
                    "Arbetsförmedlingen: the search returned ads without a "
                    "publication date; cannot split it into date windows"
                )
            # Bounds are whole seconds: the newest ad may carry a fraction
            # past ``end``, so the window ends one second later.
            end += timedelta(seconds=1)
            await self._check_date_filter(fetch, sem, end)
            self._windows = 0
            await self._walk_window(fetch, sem, start, end, absorb)
        return all_jobs

    async def _check_date_filter(
        self, fetch: Fetcher, sem: asyncio.Semaphore, end: datetime,
    ) -> None:
        """Raise unless a day in 1990 and a day a year ahead hold no ads.

        A search that ignores either bound would match ads there.
        """
        for start in (datetime(1990, 1, 1), end + timedelta(days=365)):
            window = {
                "published-after": start.strftime(_DATE_FORMAT),
                "published-before": (start + timedelta(days=1)).strftime(_DATE_FORMAT),
                "limit": 0,
            }
            payload = await self._fetch_page(fetch, sem, params=window)
            if (payload.get("total") or {}).get("value"):
                raise ScraperError(
                    f"Arbetsförmedlingen matched ads in the empty window {window}; "
                    "the search no longer filters by publication date"
                )

    async def _walk_window(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        start: datetime,
        end: datetime,
        absorb: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        """Read every ad published in ``[start, end]``."""
        self._windows += 1
        if self._windows > MAX_DATE_WINDOWS:
            raise ScraperError(
                f"Arbetsförmedlingen: the publication-date walk passed "
                f"{MAX_DATE_WINDOWS} windows; refusing to go on"
            )
        window = {
            "published-after": start.strftime(_DATE_FORMAT),
            "published-before": end.strftime(_DATE_FORMAT),
        }
        first = await self._fetch_page(
            fetch, sem, params={**window, "limit": PAGE_SIZE, "offset": 0}
        )
        total = (first.get("total") or {}).get("value", 0)
        absorb(first.get("hits") or [])
        if total <= PAGE_SIZE:
            return

        if total > WINDOW_CAP:
            # Halves overlap on their shared second so an ad stamped with a
            # fraction of a second cannot fall between them.
            middle = start + (end - start) // 2
            middle = middle.replace(microsecond=0)
            if start < middle < end:
                await asyncio.gather(
                    self._walk_window(fetch, sem, start, middle, absorb),
                    self._walk_window(fetch, sem, middle, end, absorb),
                )
                return
            log.warning(
                "Arbetsförmedlingen: %d ads published between %s and %s; "
                "only %d of them are reachable",
                total, window["published-after"], window["published-before"],
                WINDOW_CAP,
            )

        offsets = range(PAGE_SIZE, min(total, WINDOW_CAP), PAGE_SIZE)
        await asyncio.gather(*(
            self._fetch_and_absorb(
                fetch, sem,
                params={**window, "limit": PAGE_SIZE, "offset": o},
                absorb=absorb,
            )
            for o in offsets
        ))

    async def _fetch_and_absorb(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, Any],
        absorb: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        payload = await self._fetch_page(fetch, sem, params=params)
        absorb(payload.get("hits") or [])

    async def _fetch_page(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        # Retries, backoff, and 429/5xx → ScraperError mapping live in
        # the shared Fetcher.
        async with sem:
            r = await fetch.request("GET", API_URL, params=params, handled={400})
        if r.status_code == 400:
            raise ScraperError(
                f"Arbetsförmedlingen rejected {params}: {r.text[:200]}"
            )
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(
                f"Arbetsförmedlingen returned non-JSON for {params}: {exc}"
            ) from exc

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
        workload = _workload(item.get("scope_of_work"))
        if not commitment and workload is not None:
            lo, hi = workload.minimum, workload.maximum
            if lo is not None and hi is not None and hi > 0:
                commitment = f"{lo:g} %" if lo == hi else f"{lo:g}–{hi:g} %"

        primary = item.get("occupation")
        category = classification_label(primary.get("label")) if isinstance(primary, dict) else None
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
            job_category=category,
            education_requirements=_education_requirements(item),
            skill_requirements=_skill_requirements(item),
            department=department if isinstance(department, str) else None,
            employment_type=employment_type,
            commitment=commitment,
            workload_percentages=workload,
            apply_url=apply_url if isinstance(apply_url, str) and apply_url.startswith("http") else None,
            requisition_id=requisition_id,
            salary_summary=salary_summary,
            posted_at=_parse_iso(item.get("publication_date") or item.get("application_deadline")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _workload(scope: object) -> WorkloadPercentages | None:
    if not isinstance(scope, dict) or (scope.get("min") is None and scope.get("max") is None):
        return None
    try:
        return WorkloadPercentages(minimum=scope.get("min"), maximum=scope.get("max"))
    except ValidationError:
        return None


def _education_requirements(item: dict[str, Any]) -> list[EducationRequirement] | None:
    qualifications: list[EducationRequirement] = []
    for key in ("must_have", "nice_to_have"):
        block = item.get(key)
        if not isinstance(block, dict):
            continue
        for field in ("education", "education_level"):
            entries = block.get(field)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not (label := classification_label(entry.get("label"))):
                    continue
                qualification = EducationRequirement(
                    label=label, requirement="required" if key == "must_have" else "preferred",
                )
                if qualification not in qualifications:
                    qualifications.append(qualification)
    return qualifications or None


def _skill_requirements(item: dict[str, Any]) -> list[SkillRequirement] | None:
    skills: list[SkillRequirement] = []
    for key in ("must_have", "nice_to_have"):
        block = item.get(key)
        if isinstance(block, dict):
            skills.extend(skill_tags(
                block.get("skills"), label_key="label",
                requirement="required" if key == "must_have" else "preferred",
            ) or [])
    return skills or None


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


def _publication_date(payload: dict[str, Any]) -> datetime | None:
    """Publication date of the first hit, to the second, without a zone."""
    hits = payload.get("hits") or []
    if not hits or not isinstance(hits[0], dict):
        return None
    value = hits[0].get("publication_date")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None, microsecond=0)
