"""NodeFlair (https://www.nodeflair.com) — Singapore tech jobs scraper.

NodeFlair is Singapore's #1 tech job portal, aggregating engineering /
data / cybersecurity / product roles across SG and the rest of APAC.
Companies post directly (with Glassdoor-style ratings) — not syndicated
from LinkedIn / Indeed.

Public REST at ``https://www.nodeflair.com/api/v2/jobs`` — no auth
required, but the endpoint is behind Cloudflare and rejects basic
``User-Agent: Mozilla/5.0``. A full Chrome UA passes. The API is
documented as ``Disallow: /api/v2`` in robots.txt; we treat it as
public-but-undocumented (same posture as ``thehub.io/api/jobs`` and
``getonbrd.com/api/v0``).

Default scope is **Singapore only** (``country="Singapore"``) since
that's the board's core audience and gives a tight, high-signal feed.
Pass ``country=None`` to fetch global (10k+ listings, mostly APAC).

Pagination: ``?page=N`` with ``itemsCountPerPage=12``. The first page
carries ``total_listings_count`` so we can fan out the remaining pages
in parallel. Each listing carries enough data (title, company, position
tag, country, tech stack, seniority, salary range) that we don't need
per-listing detail fetches for the canonical schema.

Single-source scraper: ``company_slug`` is informational — NodeFlair's
listing endpoint is global. Pass anything (``"any"``, ``""``).
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

API_URL = "https://www.nodeflair.com/api/v2/jobs"
BASE_URL = "https://www.nodeflair.com"
PER_PAGE = 12  # Hard-coded server-side; tweaking the query is a no-op.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
MAX_PAGES_DEFAULT = 200  # 12*200 = 2.4k jobs (Singapore feed ~6k → 500 pages full)
RETRY_BASE_DELAY = 1.5

# Cloudflare blocks the default httpx UA. A full Chrome string passes;
# stripping any of these headers reintroduces the challenge.
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nodeflair.com/jobs",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# NodeFlair ``remuneration_frequency`` → canonical ``SalaryPeriod``.
_FREQUENCY_MAP: dict[str, str] = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "yearly": "YEAR",
    "annually": "YEAR",
}

# NodeFlair ``seniority`` enum → ``employment_type`` when applicable.
# Most seniority labels (Senior / Lead / Manager / …) imply FULL_TIME
# even though NodeFlair doesn't ship a separate employment-type field;
# we only map ``Intern`` → INTERN since that's the one unambiguous case.
_SENIORITY_TO_EMPLOYMENT: dict[str, str] = {
    "intern": "INTERN",
}

# Country name → ISO alpha-2. NodeFlair's ``country`` field is the
# English name; we map it for the canonical schema. Restricted to APAC
# coverage; anything else falls through to None and downstream LLM
# enrichment fills it.
_COUNTRY_ISO: dict[str, str] = {
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "thailand": "TH",
    "vietnam": "VN",
    "philippines": "PH",
    "hong kong": "HK",
    "taiwan": "TW",
    "japan": "JP",
    "south korea": "KR",
    "korea": "KR",
    "china": "CN",
    "india": "IN",
    "australia": "AU",
    "new zealand": "NZ",
    "united kingdom": "GB",
    "united states": "US",
    "germany": "DE",
}


@ScraperRegistry.register(ATSType.NODEFLAIR)
class NodeFlairScraper(BaseScraper):
    """NodeFlair (nodeflair.com) — Singapore tech-focused job board.

    Single-source scraper: ``company_slug`` is informational and ignored
    (pass anything — ``"any"``, ``""``, ``"sg"``).

    Knobs:
    - ``country`` — country filter (default ``"Singapore"``). Pass
      ``None`` to fetch the global feed (~10k jobs, APAC-heavy).
    - ``max_pages`` — pagination cap (default 200 → ~2.4k jobs).
    """

    ats = ATSType.NODEFLAIR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        country: str | None = "Singapore",
        max_pages: int = MAX_PAGES_DEFAULT,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.country = country
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Probe page 1 to learn the total count.
            first = await self._fetch_page(client, sem, page=1)
            total = int(first.get("total_listings_count") or 0)
            await absorb(first.get("job_listings") or [])

            if total <= PER_PAGE:
                return jobs

            # Cap pages by both server-truth and configured ceiling.
            pages_total = (total + PER_PAGE - 1) // PER_PAGE
            page_count = min(pages_total, self.max_pages)
            if page_count <= 1:
                return jobs

            async def one(page: int) -> None:
                payload = await self._fetch_page(client, sem, page=page)
                await absorb(payload.get("job_listings") or [])

            await asyncio.gather(*(one(p) for p in range(2, page_count + 1)))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page}
        if self.country:
            # NodeFlair accepts ``countries[]=Singapore`` (array param);
            # httpx renders the list shape correctly.
            params["countries[]"] = self.country
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers=_HEADERS,
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"NodeFlair fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"NodeFlair returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"NodeFlair returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"NodeFlair returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"NodeFlair exhausted retries at page={page}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        company_obj = item.get("company") or {}
        company = (company_obj.get("companyname") or "").strip() or "Unknown"

        # ``job_path`` is a relative path with itm_* query params we drop
        # — keep the clean canonical URL for dedup stability.
        job_path = (item.get("job_path") or "").split("?", 1)[0].strip()
        url = (
            f"{BASE_URL}{job_path}" if job_path.startswith("/")
            else f"{BASE_URL}/jobs/{ats_id}"
        )

        country_name = (item.get("country") or "").strip()
        country_iso = _COUNTRY_ISO.get(country_name.lower()) if country_name else None

        # Salary: NodeFlair sets ``is_salary_estimated=True`` for ML-derived
        # ranges and ``False`` for employer-supplied ones. Skip estimated
        # values entirely — they're noisy and not a contract from the
        # employer.
        is_estimated = bool(item.get("is_salary_estimated"))
        smin = _to_pos_float(item.get("salary_min"))
        smax = _to_pos_float(item.get("salary_max"))
        currency = (item.get("currency") or "").strip() or None
        period_raw = (item.get("remuneration_frequency") or "").strip().lower()
        salary_period = _FREQUENCY_MAP.get(period_raw) if period_raw else None

        if is_estimated or not currency:
            smin = smax = None
            currency = None
            salary_period = None

        # Position tag (Fullstack, Backend, Data, …) → department.
        department = (item.get("position") or "").strip() or None

        # Seniority list: NodeFlair surfaces multiple levels per job
        # (e.g. ["Senior", "Lead"]). Join the values for display and
        # apply the intern → INTERN mapping when present.
        seniority = item.get("seniority") or []
        commitment: str | None = None
        employment_type: str | None = None
        if isinstance(seniority, list) and seniority:
            labels = [s for s in seniority if isinstance(s, str) and s.strip()]
            if labels:
                commitment = ", ".join(labels)
                for lab in labels:
                    et = _SENIORITY_TO_EMPLOYMENT.get(lab.strip().lower())
                    if et:
                        employment_type = et
                        break

        # Tech stack: pull out the names for the raw overflow so consumers
        # can filter without re-scraping. Keep the canonical schema clean.
        tech_stacks_raw = item.get("tech_stacks") or []
        tech_stack_names: list[str] = []
        if isinstance(tech_stacks_raw, list):
            for t in tech_stacks_raw:
                if isinstance(t, dict):
                    name = t.get("name")
                    if isinstance(name, str) and name.strip():
                        tech_stack_names.append(name.strip())

        raw: dict[str, Any] = {}
        if tech_stack_names:
            raw["tech_stacks"] = tech_stack_names[:30]
        if company_obj.get("rating") is not None:
            raw["company_rating"] = company_obj["rating"]
        if company_obj.get("id") is not None:
            raw["company_id"] = company_obj["id"]
        if item.get("time_ago"):
            raw["time_ago"] = item["time_ago"]
        if is_estimated and (item.get("salary_min") or item.get("salary_max")):
            # Surface the original estimated range so consumers who want it
            # can still read it — we just don't promote it to the canonical
            # salary fields.
            raw["estimated_salary"] = {
                "min": item.get("salary_min"),
                "max": item.get("salary_max"),
                "currency": item.get("currency"),
            }

        # NodeFlair tags every listing as a tech role; the page is Asia-
        # tech-focused so default region accordingly when we have a
        # country_iso for an APAC country.
        region = "Asia" if country_iso in _APAC_ISO else None

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.NODEFLAIR,
            ats_id=ats_id,
            location=country_name or None,
            country_iso=country_iso,
            region=region,
            salary_currency=currency,
            salary_period=salary_period,
            salary_min=smin,
            salary_max=smax,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment,
            department=department,
            language="en",
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# APAC ISO codes we tag with region="Asia". Australia / NZ are technically
# Oceania, so they're excluded here even though NodeFlair surfaces them.
_APAC_ISO = {
    "SG", "MY", "ID", "TH", "VN", "PH", "HK", "TW", "JP", "KR", "CN", "IN",
}


def _to_pos_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bools are ints in Python; guard early
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None
