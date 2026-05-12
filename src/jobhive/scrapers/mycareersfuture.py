"""MyCareersFuture (Singapore government job board) scraper.

MyCareersFuture is Singapore's federal job board, operated by Workforce
Singapore (WSG). It exposes a clean, fully-open JSON API at:

    GET https://api.mycareersfuture.gov.sg/v2/jobs?limit=N&offset=N

No authentication, no API key, no captcha. ``total`` is reported on
every page (currently ~87k active listings spanning private and public
sector). Pagination is server-side via ``limit`` / ``offset``; the API
accepts ``limit`` up to 100 in practice. We page through with an async
semaphore for throughput, retrying 429/5xx with exponential backoff.

Like USAJOBS / EURES / Bundesagentur, this is a *single source* — there
is no per-tenant slug. We accept any ``company_slug`` argument (used for
logging only) and pull the full active dataset on every fetch.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://api.mycareersfuture.gov.sg/v2/jobs"
PAGE_SIZE = 100  # API accepts up to 100 per page.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
ENV_USER_AGENT = "MYCAREERSFUTURE_USER_AGENT"
DEFAULT_USER_AGENT = "stapply-ai (open-source jobs dataset)"
ENV_MAX_PAGES = "MYCAREERSFUTURE_MAX_PAGES"
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 10_000
MAX_RAW_SERIALIZED = 5_000

# Singapore-specific posting URL template — used only as a fallback when
# the API's ``metadata.jobDetailsUrl`` is missing. Both forms (with and
# without the slug prefix) resolve on the live site.
JOB_URL_TEMPLATE = "https://www.mycareersfuture.gov.sg/job/{uuid}"

# Map MyCareersFuture's ``employmentType`` strings to the canonical enum.
_EMP_TYPE_MAP = {
    "permanent": "FULL_TIME",
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "flexi-work": "TEMPORARY",
    "flexi work": "TEMPORARY",
    "internship": "INTERN",
    "internship/attachment": "INTERN",
}

# Map salaryType labels to the canonical period enum.
_SALARY_PERIOD_MAP = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "yearly": "YEAR",
    "annual": "YEAR",
    "annually": "YEAR",
}


@ScraperRegistry.register(ATSType.MYCAREERSFUTURE)
class MyCareersFutureScraper(BaseScraper):
    """Singapore MyCareersFuture scraper. Single-source — ``company_slug``
    is unused (kept for logging consistency with other scrapers).

    Optional env vars:
      - ``MYCAREERSFUTURE_USER_AGENT`` overrides the User-Agent header.
      - ``MYCAREERSFUTURE_MAX_PAGES`` caps the number of pages fetched
        (handy in dev / smoke tests; defaults to "as many as needed").
    """

    ats = ATSType.MYCAREERSFUTURE

    def fetch(self) -> list[Job]:
        user_agent = os.environ.get(ENV_USER_AGENT, DEFAULT_USER_AGENT)
        max_pages_env = os.environ.get(ENV_MAX_PAGES, "").strip()
        max_pages: int | None = None
        if max_pages_env:
            try:
                max_pages = max(1, int(max_pages_env))
            except ValueError:
                max_pages = None
        return asyncio.run(self._fetch_async(user_agent, max_pages))

    async def _fetch_async(
        self, user_agent: str, max_pages: int | None
    ) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            # First page: discover ``total`` so we can plan pagination.
            first = await self._fetch_page(client, user_agent, offset=0)
            self._absorb(first.get("results") or [], seen, jobs)
            total = int(first.get("total") or 0)
            if total <= PAGE_SIZE:
                return jobs

            offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
            if max_pages is not None:
                # First page already consumed; cap the remainder.
                offsets = offsets[: max(0, max_pages - 1)]

            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def fetch_one(offset: int) -> list[dict[str, Any]]:
                async with sem:
                    payload = await self._fetch_page(
                        client, user_agent, offset=offset
                    )
                return payload.get("results") or []

            results = await asyncio.gather(
                *(fetch_one(o) for o in offsets), return_exceptions=False
            )
            for batch in results:
                self._absorb(batch, seen, jobs)
        return jobs

    def _absorb(
        self,
        items: list[dict[str, Any]],
        seen: set[str],
        out: list[Job],
    ) -> None:
        for item in items:
            job = self._parse_job(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            out.append(job)

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        *,
        offset: int,
    ) -> dict[str, Any]:
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        params = {"limit": PAGE_SIZE, "offset": offset}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    API_URL, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"MyCareersFuture fetch failed at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"MyCareersFuture returned non-JSON at offset={offset}: {exc}"
                    ) from exc
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"MyCareersFuture returned {response.status_code} at "
                        f"offset={offset} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"MyCareersFuture returned {response.status_code} at "
                f"offset={offset}"
            )
        raise ScraperError(
            f"MyCareersFuture exhausted retries at offset={offset}: {last_exc}"
        )

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        if not isinstance(item, dict):
            return None
        ats_id = str(item.get("uuid") or "").strip()
        title = (item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        url = _job_url(item, ats_id)
        if not url:
            return None

        company = _company_name(item)
        if not company:
            return None

        description = _html_to_text(item.get("description"))

        emp_types = item.get("employmentTypes") or []
        emp_label: str | None = None
        if isinstance(emp_types, list) and emp_types:
            first_emp = emp_types[0]
            if isinstance(first_emp, dict):
                emp_label = first_emp.get("employmentType")
        employment_type = _map_employment_type(emp_label)

        salary = item.get("salary") or {}
        salary_min = _to_float(salary.get("minimum")) if isinstance(
            salary, dict
        ) else None
        salary_max = _to_float(salary.get("maximum")) if isinstance(
            salary, dict
        ) else None
        salary_period: str | None = None
        salary_summary: str | None = None
        salary_currency: str | None = None
        if salary_min is not None or salary_max is not None:
            salary_currency = "SGD"
            stype = salary.get("type") if isinstance(salary, dict) else None
            if isinstance(stype, dict):
                salary_period = _map_salary_period(stype.get("salaryType"))
            salary_summary = _format_salary_summary(
                salary_min, salary_max, salary_period
            )

        addr = item.get("address") or {}
        location = _format_location(addr)
        lat = _to_float(addr.get("lat")) if isinstance(addr, dict) else None
        lon = _to_float(addr.get("lng")) if isinstance(addr, dict) else None
        is_overseas = (
            bool(addr.get("isOverseas")) if isinstance(addr, dict) else False
        )
        # Overseas postings still get country_iso=SG since the listing
        # itself is published on the Singapore board; consumers can use
        # ``location`` / ``raw.overseasCountry`` to refine.
        country_iso = "SG"

        metadata = item.get("metadata") or {}
        posted_at = _parse_iso(
            metadata.get("createdAt") if isinstance(metadata, dict) else None
        )
        if posted_at is None and isinstance(metadata, dict):
            posted_at = _parse_iso(metadata.get("originalPostingDate"))
        requisition_id = None
        if isinstance(metadata, dict):
            raw_req = metadata.get("jobPostId")
            if isinstance(raw_req, str) and raw_req.strip():
                requisition_id = raw_req.strip()

        experience = item.get("minimumYearsExperience")
        if not isinstance(experience, int):
            experience = None

        raw = _build_raw(item, is_overseas=is_overseas)

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.MYCAREERSFUTURE,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Asia",
            lat=lat,
            lon=lon,
            employment_type=employment_type,
            commitment=emp_label if isinstance(emp_label, str) else None,
            requisition_id=requisition_id,
            description=description,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language="en",
            raw=raw or None,
        )


def _job_url(item: dict[str, Any], uuid_: str) -> str | None:
    """Pick the best public posting URL for a job.

    Order of preference: the human-readable ``metadata.jobDetailsUrl``
    (pretty slug + uuid), then the canonical bare-uuid URL. The API's
    ``_links.self.href`` is an API endpoint, not a user-facing page —
    we only fall back to the synthesized site URL.
    """
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        details = metadata.get("jobDetailsUrl")
        if (
            isinstance(details, str)
            and details.startswith("https://www.mycareersfuture.gov.sg/")
        ):
            return details
    return JOB_URL_TEMPLATE.format(uuid=uuid_)


def _company_name(item: dict[str, Any]) -> str | None:
    """Prefer the hiring employer; fall back to the posting employer.

    Many MCF rows have ``hiringCompany=null`` because the posting
    employer (often a recruitment agency) is itself the hirer. The
    name is the human-readable display label.
    """
    for key in ("hiringCompany", "postedCompany"):
        node = item.get(key)
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _format_location(addr: object) -> str | None:
    """Build a human-readable location string from the ``address`` dict.

    For Singapore-local postings the API gives us a structured block
    (``block``, ``street``, ``postalCode``) plus a ``districts`` array
    with planning-region labels. We pick the district label when
    available — it's the closest analog to "neighborhood, Singapore"
    that consumers expect. Overseas postings expose ``overseasCountry``
    instead and we use that verbatim.
    """
    if not isinstance(addr, dict):
        return "Singapore"
    if addr.get("isOverseas"):
        country = addr.get("overseasCountry")
        if isinstance(country, str) and country.strip():
            return country.strip()
        return "Overseas"
    districts = addr.get("districts") or []
    if isinstance(districts, list) and districts:
        first = districts[0]
        if isinstance(first, dict):
            label = first.get("location")
            if isinstance(label, str) and label.strip():
                return f"{label.strip()}, Singapore"
    return "Singapore"


def _map_employment_type(label: object) -> str | None:
    if not isinstance(label, str) or not label.strip():
        return None
    return _EMP_TYPE_MAP.get(label.strip().lower())


def _map_salary_period(label: object) -> str | None:
    if not isinstance(label, str) or not label.strip():
        return None
    return _SALARY_PERIOD_MAP.get(label.strip().lower())


def _format_salary_summary(
    salary_min: float | None,
    salary_max: float | None,
    period: str | None,
) -> str | None:
    period_label = {
        "HOUR": "/hour",
        "DAY": "/day",
        "WEEK": "/week",
        "MONTH": "/month",
        "YEAR": "/year",
    }.get(period or "", "")
    if salary_min is not None and salary_max is not None:
        return f"SGD {int(salary_min):,} – {int(salary_max):,}{period_label}".strip()
    if salary_min is not None:
        return f"SGD {int(salary_min):,}+{period_label}".strip()
    if salary_max is not None:
        return f"up to SGD {int(salary_max):,}{period_label}".strip()
    return None


def _build_raw(
    item: dict[str, Any], *, is_overseas: bool
) -> dict[str, Any]:
    """Pluck the small set of MCF-specific fields worth preserving.

    Constraints:
      - skills → list of plain strings (drop uuid / confidence noise)
      - keep ssoc / occupation taxonomy codes (useful for downstream joins)
      - keep flexible-work + schemes (Singapore-specific signals)
      - serialized size capped at ``MAX_RAW_SERIALIZED`` to stay within
        the schema's ~5 kB raw budget
    """
    raw: dict[str, Any] = {}
    skills = item.get("skills") or []
    if isinstance(skills, list):
        names = [
            s.get("skill").strip()
            for s in skills
            if isinstance(s, dict)
            and isinstance(s.get("skill"), str)
            and s.get("skill", "").strip()
        ]
        if names:
            raw["skills"] = names[:25]  # hard cap; nobody needs 100 skills
    for key in ("ssocCode", "occupationId", "ssocVersion"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = value.strip()
    schemes = item.get("schemes")
    if isinstance(schemes, list) and schemes:
        raw["schemes"] = schemes[:10]
    fwa = item.get("flexibleWorkArrangements")
    if isinstance(fwa, list) and fwa:
        raw["flexibleWorkArrangements"] = fwa[:10]
    categories = item.get("categories")
    if isinstance(categories, list) and categories:
        cat_names = [
            c.get("category").strip()
            for c in categories
            if isinstance(c, dict)
            and isinstance(c.get("category"), str)
            and c.get("category", "").strip()
        ]
        if cat_names:
            raw["categories"] = cat_names[:10]
    position_levels = item.get("positionLevels")
    if isinstance(position_levels, list) and position_levels:
        level_names = [
            p.get("position").strip()
            for p in position_levels
            if isinstance(p, dict)
            and isinstance(p.get("position"), str)
            and p.get("position", "").strip()
        ]
        if level_names:
            raw["positionLevels"] = level_names[:5]
    if is_overseas:
        raw["isOverseas"] = True
    # Defensive size cap — re-serialize and trim aggressively if needed.
    if raw:
        import json
        serialized = json.dumps(raw, default=str)
        if len(serialized) > MAX_RAW_SERIALIZED:
            # Drop the chattiest field first.
            raw.pop("skills", None)
    return raw


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text[:MAX_DESCRIPTION_LEN] if text else None


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
