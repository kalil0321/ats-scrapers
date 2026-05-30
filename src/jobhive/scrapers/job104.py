"""104.com.tw — Taiwan's largest job board scraper.

104 (https://www.104.com.tw) is by far the largest job board in Taiwan,
with ~516k live postings at the time of writing. The public search API
that the website's own frontend consumes is unauthenticated:

    GET https://www.104.com.tw/jobs/search/api/jobs

The API hard-caps ``lastPage`` at **100** (×32 jobs per page = 3,200
jobs per query) regardless of how many results match — so to cover the
full corpus we **slice** the search via ``area`` and ``jobcat`` filters.
A single (area, top-level jobcat) slice is virtually always under the
3,200 cap, even for the biggest combinations (Taipei × Software).

22 areas × ~20 top-level job categories = ~440 slice queries × up to
100 pages each = ~44k requests in the worst case. Concurrency is
capped at 4 to stay polite — same default as the Wanted scraper.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / wanted pattern). Output rows
carry the publishing employer's name as ``company`` so cross-ATS
dedup against direct ATS scrapes still works.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://www.104.com.tw/jobs/search/api/jobs"
SEARCH_REFERER = "https://www.104.com.tw/jobs/search/"
PAGE_SIZE = 32  # API's fixed page size — overriding doesn't increase it.
PAGE_LIMIT = 100  # ``lastPage`` is capped here, regardless of ``total``.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Top-level area codes (Taiwan's cities/counties + cross-strait + overseas)
# as used by 104's ``area=`` filter. These are 104-internal codes, not
# ROC administrative codes — verified against the live search UI.
_DEFAULT_AREAS: tuple[str, ...] = (
    "6001001000",  # Taipei City (台北市)
    "6001002000",  # New Taipei City (新北市)
    "6001008000",  # Taoyuan
    "6001006000",  # Hsinchu County (新竹縣)
    "6001005000",  # Hsinchu City (新竹市)
    "6001007000",  # Miaoli
    "6001004000",  # Taichung
    "6001003000",  # Yilan
    "6001011000",  # Changhua
    "6001012000",  # Nantou
    "6001013000",  # Yunlin
    "6001014000",  # Chiayi
    "6001015000",  # Tainan
    "6001016000",  # Kaohsiung
    "6001017000",  # Pingtung
    "6001018000",  # Taitung
    "6001019000",  # Hualien
    "6001020000",  # Penghu / Kinmen / Matsu
    "6001025000",  # China (cross-strait)
    "6001026000",  # Overseas
)

# Areas that are NOT inside Taiwan — used to suppress the default
# ``country_iso=TW`` on rows scraped through these slices.
_NON_TW_AREAS: frozenset[str] = frozenset({"6001025000", "6001026000"})

# Top-level job categories (104's ``jobcat=`` filter).
_DEFAULT_JOBCATS: tuple[str, ...] = (
    "2001000000",  # Operations / 管理
    "2002000000",  # Sales / 銷售
    "2003000000",  # Marketing / 行銷
    "2004000000",  # Admin / 行政
    "2005000000",  # Finance / 財務
    "2006000000",  # Manufacturing / 製造
    "2007000000",  # Software / 資訊
    "2008000000",  # Engineering / 工程
    "2009000000",  # Construction / 建築
    "2010000000",  # Hospitality / 餐旅
    "2011000000",  # Healthcare / 醫療
    "2012000000",  # Education / 教育
    "2013000000",  # Design / 設計
    "2014000000",  # Legal / 法務
    "2015000000",  # Logistics / 物流
    "2016000000",  # Media / 媒體
    "2017000000",  # Beauty / 美容
    "2018000000",  # Maintenance / 維修
    "2019000000",  # General / 總務
    "2099000000",  # Other / 其他
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@ScraperRegistry.register(ATSType.JOB104)
class Job104Scraper(BaseScraper):
    """104.com.tw search API scraper.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper enumerates every (area, jobcat)
    slice covering all of Taiwan (+ cross-strait + overseas).

    To restrict the sweep (smaller test runs, partial-region cron):

    .. code-block:: python

        Job104Scraper(
            "any",
            areas=["6001001000"],          # Taipei only
            jobcats=["2007000000"],        # Software only
        )
    """

    ats = ATSType.JOB104

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        areas: tuple[str, ...] | list[str] = _DEFAULT_AREAS,
        jobcats: tuple[str, ...] | list[str] = _DEFAULT_JOBCATS,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.areas = tuple(areas)
        self.jobcats = tuple(jobcats)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]], *, area: str) -> None:
            new_jobs: list[Job] = []
            async with lock:
                for it in items:
                    job = self._parse_job(it, area=area)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    new_jobs.append(job)
            jobs.extend(new_jobs)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_slice(area: str, jobcat: str) -> None:
                await self._exhaust_slice(
                    client, sem,
                    area=area, jobcat=jobcat,
                    absorb=absorb,
                )

            await _gather_tolerant(
                (per_slice(a, c) for a in self.areas for c in self.jobcats),
                label="slice",
            )
        return jobs

    async def _exhaust_slice(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        area: str,
        jobcat: str,
        absorb: Any,
    ) -> None:
        """Pull every page of a (area, jobcat) slice. Caps at PAGE_LIMIT
        — if a single (area, jobcat) ever produces >3,200 jobs we accept
        the cap loss for the first PR (rare; would need sub-jobcat
        recursion to fully cover)."""
        first = await self._search(
            client, sem, area=area, jobcat=jobcat, page=1,
        )
        items = (first.get("data") or [])
        if not items:
            return
        await absorb(items, area=area)

        pagination = (first.get("metadata") or {}).get("pagination") or {}
        try:
            last_page = int(pagination.get("lastPage") or 1)
        except (TypeError, ValueError):
            last_page = 1
        last_page = min(last_page, PAGE_LIMIT)
        if last_page <= 1:
            return

        async def one(page: int) -> None:
            payload = await self._search(
                client, sem, area=area, jobcat=jobcat, page=page,
            )
            await absorb(payload.get("data") or [], area=area)

        await _gather_tolerant(
            (one(p) for p in range(2, last_page + 1)),
            label="page",
        )

    # --- HTTP layer ---------------------------------------------------------

    async def _search(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        area: str,
        jobcat: str,
        page: int,
    ) -> dict[str, Any]:
        params = {
            "order": "15",  # sort by posted-desc (newest first)
            "asc": "0",
            "page": str(page),
            "mode": "s",
            "jobsource": "2018indexpoc",
            "area": area,
            "jobcat": jobcat,
            "ro": "0",  # all work types
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL,
                        params=params,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0",
                            "Referer": SEARCH_REFERER,
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"104 fetch failed for area={area} "
                            f"jobcat={jobcat} page={page}: {exc}"
                        ) from exc
                    response = None  # release sem before backing off
            if response is None:
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"104 returned non-JSON for area={area} "
                        f"jobcat={jobcat} page={page}: {exc}"
                    ) from exc
            if response.status_code in (400, 404, 422):
                # Invalid slice (unknown category code, etc.) — treat as
                # "this slice has no data" rather than aborting the run.
                log.warning(
                    "104 returned %s for area=%s jobcat=%s page=%s — "
                    "skipping slice", response.status_code, area, jobcat, page,
                )
                return {"data": [], "metadata": {"pagination": {"lastPage": 1}}}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"104 returned {response.status_code} for area={area} "
                        f"jobcat={jobcat} page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"104 returned {response.status_code} for area={area} "
                f"jobcat={jobcat} page={page}"
            )
        raise ScraperError(
            f"104 exhausted retries for area={area} jobcat={jobcat} "
            f"page={page}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_job(
        self, item: dict[str, Any], *, area: str,
    ) -> Job | None:
        job_no = item.get("jobNo")
        if job_no is None or str(job_no).strip() == "":
            return None
        ats_id = str(job_no).strip()

        title = (item.get("jobName") or "").strip()
        if not title:
            return None

        company = (item.get("custName") or "").strip() or "Unknown"

        url = _absolute_job_url(item.get("link") or {}, ats_id)
        if url is None:
            return None

        # Location: 104 ships a Chinese city/district label
        # (``jobAddrNoDesc``) plus an optional free-text street address.
        # Combine when both are present so consumers see e.g.
        # "新竹縣竹北市 — 光明六路 100 號" instead of dropping detail.
        addr_desc = _clean_str(item.get("jobAddrNoDesc"))
        street = _clean_str(item.get("jobAddress"))
        if addr_desc and street and street != addr_desc:
            location = f"{addr_desc} — {street}"
        else:
            location = addr_desc or street

        # ``country_iso`` defaults to TW unless this slice is cross-strait
        # (China) or overseas — the area filter is structured enough to
        # decide without reading the location text.
        country_iso = None if area in _NON_TW_AREAS else "TW"

        lat = _to_float(item.get("lat"))
        lon = _to_float(item.get("lon"))
        # 104 occasionally ships (0.0, 0.0) for jobs whose employer hasn't
        # been geocoded — drop those rather than pin them to Africa.
        if lat == 0.0 and lon == 0.0:
            lat = lon = None

        salary_min = _to_float(item.get("salaryLow"))
        salary_max = _to_float(item.get("salaryHigh"))
        # 待遇面議 ("negotiable") ships salaryLow=0/salaryHigh=0 — treat a
        # zero (or absent) bound as unknown rather than emitting "TWD 0–0".
        if not salary_min:
            salary_min = None
        if not salary_max:
            salary_max = None
        salary_currency: str | None = None
        salary_summary: str | None = None
        if salary_min is not None or salary_max is not None:
            salary_currency = "TWD"
            salary_summary = _format_salary_summary(salary_min, salary_max)

        posted_at = _parse_appear_date(item.get("appearDate"))

        description = _clean_description(item.get("description")) or _clean_description(
            item.get("descSnippet")
        )

        department = _first_jobcat_name(item.get("jobCat"))

        raw: dict[str, Any] = {}
        for src_key, dst_key in (
            ("jobAddrNo", "job_addr_no"),
            ("coIndustry", "industry"),
            ("coIndustryDesc", "industry_desc"),
            ("optionEdu", "education"),
            ("period", "experience_label"),
            ("jobType", "job_type"),
            ("employeeCount", "employee_count"),
            ("remoteWorkType", "remote_work_type"),
        ):
            value = item.get(src_key)
            if value not in (None, "", []):
                raw[dst_key] = value
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            raw["tags"] = [t for t in tags if isinstance(t, str)][:20]
        skills = item.get("pcSkills")
        if isinstance(skills, list) and skills:
            raw["skills"] = [
                s.get("description") if isinstance(s, dict) else s
                for s in skills
                if isinstance(s, dict | str)
            ][:20]

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.JOB104,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            lat=lat,
            lon=lon,
            salary_currency=salary_currency,
            salary_period="MONTH" if salary_currency else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            department=department,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language="zh",
            raw=raw or None,
        )


def _absolute_job_url(link: dict[str, Any], job_no: str) -> str | None:
    """104 ships ``link.job`` as a protocol-relative URL
    (``//www.104.com.tw/job/abc123``). Prefix ``https:`` so downstream
    Pydantic ``HttpUrl`` validation accepts it. Falls back to
    constructing the canonical URL from ``job_no``."""
    raw = link.get("job") if isinstance(link, dict) else None
    if isinstance(raw, str) and raw.strip():
        raw = raw.strip()
        if raw.startswith("//"):
            return f"https:{raw}"
        if raw.startswith("http://"):
            return "https://" + raw[len("http://"):]
        if raw.startswith("https://"):
            return raw
        if raw.startswith("/"):
            return f"https://www.104.com.tw{raw}"
    return f"https://www.104.com.tw/job/{job_no}"


def _parse_appear_date(value: object) -> datetime | None:
    """``appearDate`` is a YYYYMMDD string (e.g. ``"20260510"``). Parse to
    a UTC-naive datetime at midnight, or return None if missing/malformed."""
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def _format_salary_summary(low: float | None, high: float | None) -> str:
    """Render a TWD salary range using the local "NT$" symbol —
    matches how 104 displays it in the UI."""
    if low is not None and high is not None:
        return f"NT${int(low):,} – NT${int(high):,}"
    if low is not None:
        return f"NT${int(low):,}+"
    assert high is not None  # one bound must be set if we got here
    return f"up to NT${int(high):,}"


def _first_jobcat_name(value: object) -> str | None:
    """Top-level job category name from ``jobCat`` (a list of
    ``{"name": ..., "code": ...}``). Returns the first non-empty name."""
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _clean_description(value: object) -> str | None:
    """Strip HTML tags and collapse whitespace from 104's ``description``
    field. Some postings ship plain text, some ship light HTML."""
    if not isinstance(value, str):
        return None
    stripped = _HTML_TAG_RE.sub(" ", value)
    cleaned = _WS_RE.sub(" ", stripped).strip()
    return cleaned or None


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


async def _gather_tolerant(coros: Any, *, label: str) -> None:
    """Run every coroutine concurrently, log + swallow failures instead
    of cancelling siblings — matches the EURES pattern. A single 104
    slice failure shouldn't abort a 440-slice run."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            log.warning("104 %s subtask failed: %s", label, r)
