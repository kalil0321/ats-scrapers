"""SmartRecruiters scraper.

Listing API (no auth, paginated):
    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
        ?limit=100&offset={n}

Detail API (per-job, best-effort):
    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}

The listing returns title/location/department/typeOfEmployment but
not the description body. The detail endpoint adds ``jobAd.sections``
(companyDescription / jobDescription / qualifications /
additionalInformation), ``applyUrl``, and ``postingUrl``.

Detail enrichment is enabled by default so published rows carry descriptions
when the public detail endpoint exposes them.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._job_classification import classification_label
from ats_scrapers.scrapers._job_places import distinct_places, job_location
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.models import JobLocation, WorkplaceType

API_TEMPLATE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_TEMPLATE = (
    "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}"
)
PAGE_LIMIT = 100
DETAIL_CONCURRENCY = 8

# Detail enrichment is best-effort: any 4xx/5xx just skips the job, so
# hand every error status back unmapped instead of retrying/raising.
_DETAIL_HANDLED = frozenset(range(400, 600))

_TAG_RE = re.compile(r"<[^>]+>")

# ``typeOfEmployment.id`` is a stable enum.
_EMPLOYMENT_TYPE_MAP = {
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "full_time": "FULL_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "freelance": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed_term": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "apprentice": "INTERN",
    "temporary": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "casual": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.SMARTRECRUITERS)
class SmartRecruitersScraper(BaseScraper):
    ats = ATSType.SMARTRECRUITERS

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(1)
                await self._enrich_detail(fetch, sem, copy)
            job.job_category = copy.job_category
            job.workplace_type = copy.workplace_type
            job.locations = copy.locations
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        all_jobs: list[Job] = []
        offset = 0
        async with self.make_fetcher() as fetch:
            while True:
                payload = await fetch.get_json(
                    url, params={"limit": PAGE_LIMIT, "offset": offset},
                )
                content = payload.get("content", [])
                all_jobs.extend(self._parse_job(item) for item in content)
                if len(content) < PAGE_LIMIT:
                    break
                offset += PAGE_LIMIT

            if self.include_descriptions and all_jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(fetch, sem, j) for j in all_jobs
                ))
        return all_jobs

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        if not job.ats_id:
            return
        url = DETAIL_TEMPLATE.format(slug=self.company_slug, id=job.ats_id)
        async with sem:
            try:
                response = await fetch.request("GET", url, handled=_DETAIL_HANDLED)
            except ScraperError:
                return
        if response.status_code != 200:
            return
        try:
            data = response.json()
        except ValueError:
            return
        _apply_detail_to_job(job, data)

    def _parse_job(self, item: dict[str, Any]) -> Job:
        location = item.get("location") or {}
        loc_str = _format_location(location) if isinstance(location, dict) else None

        # ``location.remote`` is an explicit bool; ``country == "remote"``
        # is the legacy convention some tenants still use.
        is_remote: bool | None = None
        if isinstance(location, dict):
            remote_flag = location.get("remote")
            if (isinstance(remote_flag, bool) and remote_flag) or (
                location.get("country") == "remote"
            ):
                is_remote = True
            elif isinstance(remote_flag, bool):
                is_remote = False  # explicitly non-remote

        department = (
            item.get("department", {}).get("label")
            if isinstance(item.get("department"), dict) else None
        )

        # Function (e.g. ``Customer Service``, ``Engineering``) is the
        # closest to "team" SmartRecruiters exposes — fall through to
        # it when ``department`` is empty (~65% of rows had no dept).
        function = (
            item.get("function", {}).get("label")
            if isinstance(item.get("function"), dict) else None
        )
        team = function if isinstance(function, str) else None
        if not department and team:
            department = team
            team = None

        # ``typeOfEmployment`` ships as ``{id, label}``; the ``id`` is
        # the canonical enum (``permanent``, ``intern``, ``contract``,
        # ``temporary``…), label is the localised display string.
        type_obj = item.get("typeOfEmployment") or {}
        emp_id = type_obj.get("id") if isinstance(type_obj, dict) else None
        emp_label = type_obj.get("label") if isinstance(type_obj, dict) else None
        employment_type = _map_employment_type(emp_id) or _map_employment_type(emp_label)
        commitment = (
            emp_label.strip() if isinstance(emp_label, str) and emp_label.strip()
            else (emp_id.strip() if isinstance(emp_id, str) and emp_id.strip() else None)
        )

        raw: dict[str, Any] = {}
        for k in ("industry", "function", "department", "experienceLevel",
                  "creator", "company", "refNumber", "customField",
                  "language"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=f"https://jobs.smartrecruiters.com/{self.company_slug}/{item['id']}",
            title=item["name"],
            company=self.company_slug,
            ats_type=ATSType.SMARTRECRUITERS,
            ats_id=item["id"],
            job_category=_job_function(item.get("function")),
            location=loc_str,
            locations=distinct_places([_posting_place(location)]),
            workplace_type=_posting_mode(location),
            is_remote=is_remote,
            department=department,
            team=team,
            employment_type=employment_type,
            commitment=commitment,
            requisition_id=item.get("refNumber") or None,
            posted_at=_parse_iso(item.get("releasedDate")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _apply_detail_to_job(job: Job, detail: dict[str, Any]) -> None:
    """Hydrate ``job`` from a ``/postings/{id}`` detail payload.

    Pulls description from ``jobAd.sections`` (a dict keyed by
    ``companyDescription`` / ``jobDescription`` / ``qualifications`` /
    ``additionalInformation`` — each carrying ``title`` + HTML
    ``text``). We concatenate the four sections' plain text into a
    single body, capped at 25k chars, with the actual job description
    first so consumers see the most relevant content if truncated.
    """
    location = detail.get("location")
    if place := _posting_place(location):
        if job.locations and job.locations[0].label == place.label:
            listed = job.locations[0]
            place = place.model_copy(update={
                key: getattr(listed, key) for key in ("city", "region", "country_iso")
                if getattr(place, key) is None
            })
        job.locations = [place]
    if mode := _posting_mode(location):
        job.workplace_type = mode
    if job.job_category is None:
        job.job_category = _job_function(detail.get("function"))
    if not job.description:
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        if isinstance(sections, dict):
            parts: list[str] = []
            for key in (
                "jobDescription",
                "qualifications",
                "additionalInformation",
                "companyDescription",
            ):
                section = sections.get(key)
                if isinstance(section, dict):
                    text = section.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(_strip_html(text))
            if parts:
                job.description = "\n\n".join(parts)[:25_000]

    if not job.apply_url:
        apply_url = detail.get("applyUrl")
        if isinstance(apply_url, str) and apply_url.strip():
            job.apply_url = apply_url.strip()


def _posting_place(loc: object) -> JobLocation | None:
    if not isinstance(loc, dict):
        return None
    country = loc.get("country")
    if isinstance(country, str) and country.lower() == "remote":
        country = None
    return job_location(loc.get("fullLocation"), city=loc.get("city"), region=loc.get("region"),
                        country=country, country_code=country)


def _posting_mode(loc: object) -> WorkplaceType | None:
    if not isinstance(loc, dict):
        return None
    remote, hybrid = loc.get("remote"), loc.get("hybrid")
    if remote is True and hybrid is True:
        return None
    if hybrid is True:
        return "hybrid"
    if remote is True:
        return "remote"
    if remote is False and hybrid is False:
        return "onsite"
    return None


def _job_function(value: object) -> str | None:
    if not isinstance(value, dict) or value.get("id") == "other":
        return None
    return classification_label(value.get("label"))


def _format_location(location: dict[str, Any]) -> str | None:
    parts = [
        str(location[k]).strip()
        for k in ("city", "region", "country")
        if isinstance(location.get(k), str) and location.get(k).strip()
    ]
    return ", ".join(parts) or None


def _map_employment_type(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    norm = value.strip().lower().replace("-", "_").replace(" ", "_")
    if norm in _EMPLOYMENT_TYPE_MAP:
        return _EMPLOYMENT_TYPE_MAP[norm]
    for needle, mapped in _EMPLOYMENT_TYPE_MAP.items():
        if needle in norm:
            return mapped
    return None


def _strip_html(text: str) -> str:
    out = _TAG_RE.sub(" ", text)
    out = html_mod.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
