"""Workforce Australia (jobsearch.gov.au) scraper.

Workforce Australia is the Australian federal government's national job
board, operated by the Department of Employment and Workplace Relations.
It carries ~170k+ live postings spanning the full Australian labour
market: private-sector employers, public-sector roles, indigenous
employment, regional jobs, and apprenticeships.

The site exposes a public REST API — no auth, no API key, plain JSON:

    GET https://www.workforceaustralia.gov.au/api/v1/global/vacancies
        ?size={1..100}&page={0..N}

Each response carries ``totalCount``, ``pageNumber`` (0-indexed),
``pageSize`` and a ``results`` array of ``{score, result}`` wrappers.
The wrapped ``result`` is the actual vacancy with a stable integer
``vacancyId`` we use as ``ats_id``.

Like USAJOBS / EURES / Arbetsförmedlingen / Bundesagentur this is a
single-source scraper: there is no per-tenant slug. We accept any
``company_slug`` argument (used for logging only) and pull the full
active dataset on every fetch.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://www.workforceaustralia.gov.au/api/v1/global/vacancies"
JOB_URL_TEMPLATE = (
    "https://www.workforceaustralia.gov.au/individuals/jobs/details/{id}"
)
PAGE_SIZE = 100  # API accepts up to 100/page.
DEFAULT_MAX_PAGES = 100  # 100 pages * 100 = 10,000 jobs by default.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 10_000

# ``workType.label`` → canonical EmploymentType. The Workforce Australia
# taxonomy distinguishes work type ("Full time" / "Part time" / "Casual")
# from tenure ("Permanent" / "Contract"); we map work type first because
# it carries the FT/PT signal, then fall back to tenure for contracts.
_WORK_TYPE_MAP = {
    "full time position": "FULL_TIME",
    "full time": "FULL_TIME",
    "part time position": "PART_TIME",
    "part time": "PART_TIME",
    "casual position": "TEMPORARY",
    "casual": "TEMPORARY",
}

_TENURE_MAP = {
    "permanent position": "FULL_TIME",
    "permanent": "FULL_TIME",
    "contract position": "CONTRACT",
    "contract": "CONTRACT",
    "apprenticeship": "INTERN",
    "traineeship": "INTERN",
    "internship": "INTERN",
}


@ScraperRegistry.register(ATSType.WORKFORCEAUSTRALIA)
class WorkforceAustraliaScraper(BaseScraper):
    """Workforce Australia (jobsearch.gov.au) — single-source scraper.

    ``company_slug`` is accepted for interface symmetry but ignored; the
    scraper always pulls the full active dataset. ``max_pages`` caps the
    pagination depth (default 100 → ~10,000 jobs).
    """

    ats = ATSType.WORKFORCEAUSTRALIA

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # First page tells us totalCount; we use it to size the
            # remaining concurrent fetches.
            first = await self._fetch_page(client, sem, page=0)
            for item in first.get("results") or []:
                job = self._parse(item)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)

            total = int(first.get("totalCount") or 0)
            if total <= PAGE_SIZE:
                return jobs

            # Cap total pages at both the API-implied count and the
            # configured ceiling.
            api_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            page_count = min(api_pages, self.max_pages)
            remaining_pages = list(range(1, page_count))
            if not remaining_pages:
                return jobs

            payloads = await asyncio.gather(*(
                self._fetch_page(client, sem, page=p) for p in remaining_pages
            ))
            for payload in payloads:
                items = payload.get("results") or []
                if not items:
                    continue
                for item in items:
                    job = self._parse(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        params = {"size": PAGE_SIZE, "page": page}
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers=headers
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Workforce Australia returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Workforce Australia returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2**attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Workforce Australia returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"Workforce Australia exhausted retries at page={page}: {last_exc}"
        )

    def _parse(self, wrapper: dict[str, Any]) -> Job | None:
        """Parse a single ``{score, result}`` envelope into a ``Job``."""
        if not isinstance(wrapper, dict):
            return None
        result = wrapper.get("result")
        if not isinstance(result, dict):
            return None

        vacancy_id = result.get("vacancyId")
        if vacancy_id is None:
            return None
        ats_id = str(vacancy_id).strip()
        title = (result.get("title") or "").strip()
        if not ats_id or not title:
            return None

        url = JOB_URL_TEMPLATE.format(id=ats_id)

        company = (
            (result.get("employerName") or "").strip()
            or _label(result.get("organisation"))
            or "Unknown"
        )

        location = _format_location(
            result.get("suburb"), result.get("state"), result.get("postCode")
        )

        lat = _coerce_coord(result.get("latitude"))
        lon = _coerce_coord(result.get("longitude"))

        salary_summary = _label(result.get("salary")) or None
        salary_currency = "AUD" if salary_summary else None

        description = _html_to_text(result.get("description"))

        industry_label = _label(result.get("industry"))
        occupation_label = _label(result.get("occupation"))
        department = industry_label or occupation_label
        team = (
            occupation_label
            if occupation_label and occupation_label != department
            else None
        )

        work_type_label = _label(result.get("workType"))
        tenure_label = _label(result.get("tenure"))
        contract_type_label = _label(result.get("contractType"))
        job_type_label = _label(result.get("jobType"))
        employment_type = _map_employment_type(
            work_type_label, tenure_label, contract_type_label, job_type_label
        )

        commitment = (
            contract_type_label
            or tenure_label
            or work_type_label
            or None
        )

        requisition_id_raw = result.get("referenceCode")
        requisition_id = (
            str(requisition_id_raw).strip()
            if isinstance(requisition_id_raw, str)
            and requisition_id_raw.strip()
            else None
        )

        raw: dict[str, object] = {}
        site_label = _label(result.get("site"))
        if site_label:
            raw["site"] = site_label
        if isinstance(result.get("isIndigenousJob"), bool):
            raw["is_indigenous"] = result["isIndigenousJob"]
        if isinstance(result.get("isExternalJob"), bool):
            raw["is_external"] = result["isExternalJob"]
        positions = result.get("positionsAvailable")
        if isinstance(positions, int):
            raw["positions_available"] = positions
        how_to_apply = result.get("howToApplyCode")
        if isinstance(how_to_apply, str) and how_to_apply.strip():
            raw["how_to_apply"] = how_to_apply.strip()

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.WORKFORCEAUSTRALIA,
            ats_id=ats_id,
            location=location,
            country_iso="AU",
            lat=lat,
            lon=lon,
            description=description,
            department=department,
            team=team,
            employment_type=employment_type,
            commitment=commitment,
            requisition_id=requisition_id,
            salary_summary=salary_summary,
            salary_currency=salary_currency,
            language="en",
            posted_at=_parse_iso(result.get("creationDate")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _label(value: object) -> str | None:
    """Extract ``label`` from a Workforce Australia ``{code, label}`` dict."""
    if isinstance(value, dict):
        label = value.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    elif isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _format_location(
    suburb: object, state: object, postcode: object
) -> str | None:
    """Build a human-readable location like "Sydney, NSW 2000"."""
    suburb_s = (
        suburb.strip().title()
        if isinstance(suburb, str) and suburb.strip()
        else None
    )
    state_s = (
        state.strip().upper()
        if isinstance(state, str) and state.strip()
        else None
    )
    postcode_s = (
        postcode.strip()
        if isinstance(postcode, str) and postcode.strip()
        else None
    )

    if suburb_s and state_s and postcode_s:
        return f"{suburb_s}, {state_s} {postcode_s}"
    if suburb_s and state_s:
        return f"{suburb_s}, {state_s}"
    if state_s and postcode_s:
        return f"{state_s} {postcode_s}"
    return suburb_s or state_s or postcode_s


def _coerce_coord(value: object) -> float | None:
    """Coerce a latitude/longitude string or number to a non-zero float."""
    if value in (None, "", 0, 0.0):
        return None
    try:
        coord = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if coord == 0.0:
        return None
    return coord


def _map_employment_type(
    work_type: str | None,
    tenure: str | None,
    contract_type: str | None,
    job_type: str | None,
) -> str | None:
    """Coerce Workforce Australia labels to the shared enum.

    ``workType`` carries the FT/PT/Casual signal; tenure carries
    Permanent/Contract/Apprenticeship; contract_type and job_type
    occasionally surface labels (e.g. internships) too. We check in
    that order so a "Full time Permanent" role lands on FULL_TIME
    rather than CONTRACT.
    """
    for label in (work_type, contract_type, job_type):
        if isinstance(label, str):
            key = label.strip().lower()
            mapped = _WORK_TYPE_MAP.get(key)
            if mapped:
                return mapped
    if isinstance(tenure, str):
        key = tenure.strip().lower()
        mapped = _TENURE_MAP.get(key)
        if mapped:
            return mapped
    return None


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:MAX_DESCRIPTION_LEN]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
