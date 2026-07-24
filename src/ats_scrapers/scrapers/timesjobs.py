"""TimesJobs (https://www.timesjobs.com) — Indian hybrid jobboard scraper.

TimesJobs (a Times Internet property) is one of India's largest job
boards, alongside Naukri and Foundit/Monster. The site recently
migrated to a Next.js SPA backed by a public JSON API at
``tjapi.timesjobs.com`` which the frontend calls via:

    POST https://tjapi.timesjobs.com/search/api/v1/search/jobs/list

The endpoint is unauthenticated and accepts page-based pagination via
``{"keyword": " ", "location": "", "page": "1", "size": "100"}``. An
empty ``keyword`` returns HTTP 400 ("Invalid JSON format"); a single
space matches every active listing (~200 k jobs at any given time).

Page size limit appears generous (we successfully request ``size=1000``
in probes), but we stay at 100/page to keep individual responses small
and the server polite.

The list response carries enough fields per row that we don't need to
fetch the per-job detail endpoint
(``/job-api/api/jobs/public/{jobId}``) — title, company, location,
truncated description, skills, experience range, posted date, and a
public ``jobDetailUrl`` are all present on the list payload.

Single-source scraper: ``company_slug`` is informational and ignored.
Output rows carry the publishing employer's name as ``company`` so
cross-ATS dedup against the same role on Workday/Greenhouse still works.

Although TimesJobs is an Indian board, the inventory mixes India-based
roles with global postings (Google in Taiwan, Hilton in the US, …) —
so ``country_iso`` is left ``None`` and the downstream LLM enrichment
derives it from the ``location`` string instead of hard-coding ``IN``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://tjapi.timesjobs.com/search/api/v1/search/jobs/list"
# A single space matches every active listing. An empty keyword returns
# HTTP 400 from the API.
WILDCARD_KEYWORD = " "
PAGE_SIZE = 100
MAX_PAGES = 5000  # Safety belt — live board is ~2 k pages at 100/page.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.timesjobs.com",
    "Referer": "https://www.timesjobs.com/",
    "User-Agent": "Mozilla/5.0",
}

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.TIMESJOBS)
class TimesJobsScraper(BaseScraper):
    """TimesJobs (timesjobs.com) — Indian hybrid job board.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"india"``) — the scraper enumerates the entire
    active board.
    """

    ats = ATSType.TIMESJOBS

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.max_pages = max_pages

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        """Legacy in-memory fetch — accumulates the full corpus into a
        list. At ~200 k jobs that's a few hundred MB; for cron contexts
        that write straight to disk prefer :meth:`fetch_stream`."""
        return self._run_sync(self.afetch())

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Same producer-queue-consumer pattern as
        :meth:`ats_scrapers.scrapers.eures.EuresScraper.fetch_stream` — keeps
        memory bounded regardless of corpus size."""
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

    async def _fetch_async(
        self,
        *,
        on_job: Callable[[Job], Awaitable[None]] | None = None,
    ) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
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

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=self.proxy
        ) as client:
            # Fetch page 1 synchronously so we know the total count and
            # can fan-out the remaining pages concurrently. The API
            # returns ``totalPages`` as a float, so coerce to int.
            first = await self._search(client, page=1)
            await absorb(first.get("jobs") or [])

            total_pages_raw = first.get("totalPages")
            if total_pages_raw is None:
                raise ScraperError(
                    "TimesJobs response omitted required totalPages"
                )
            try:
                total_pages = int(total_pages_raw)
            except (TypeError, ValueError) as exc:
                raise ScraperError(
                    f"TimesJobs returned invalid totalPages={total_pages_raw!r}"
                ) from exc
            if total_pages < 1:
                raise ScraperError(
                    f"TimesJobs returned invalid totalPages={total_pages}"
                )
            if self.max_pages is not None:
                total_pages = min(total_pages, self.max_pages)
            elif total_pages > MAX_PAGES:
                raise ScraperError(
                    f"TimesJobs reports {total_pages} pages, above the "
                    f"validated safety limit of {MAX_PAGES}; refusing a "
                    "silent partial scrape"
                )
            if total_pages <= 1:
                return all_jobs

            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def one(page: int) -> None:
                async with sem:
                    payload = await self._search(client, page=page)
                await absorb(payload.get("jobs") or [])

            tasks = [
                asyncio.create_task(one(page))
                for page in range(2, total_pages + 1)
            ]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        return all_jobs

    async def _search(
        self, client: httpx.AsyncClient, *, page: int
    ) -> dict[str, Any]:
        body = {
            "keyword": WILDCARD_KEYWORD,
            "location": "",
            "page": str(page),
            "size": str(PAGE_SIZE),
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await client.post(API_URL, json=body, headers=_HEADERS)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"TimesJobs fetch failed (page={page}): {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue

            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"TimesJobs returned non-JSON for page={page}: {exc}"
                    ) from exc
                if not isinstance(data, dict):
                    raise ScraperError(
                        f"TimesJobs API shape changed — expected a dict, "
                        f"got {type(data).__name__}"
                    )
                return data

            if r.status_code in (429,) or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"TimesJobs returned {r.status_code} after "
                        f"{MAX_RETRIES} retries (page={page})"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue

            raise ScraperError(
                f"TimesJobs returned {r.status_code} (page={page}): "
                f"{r.text[:120]}"
            )

        raise ScraperError(
            f"TimesJobs exhausted retries (page={page}): {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("jobId") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("jobDetailUrl") or "").strip()
        if not (ats_id and title and url):
            return None

        company = (item.get("company") or item.get("hfCompany") or "").strip() or "Unknown"
        location = _clean_location(item.get("location"))

        # Salary: TimesJobs uses -1 to indicate "not disclosed", and the
        # currency is always present (mostly INR). Only emit salary
        # fields when a real range is provided.
        salary_min, salary_max = _clean_salary_range(
            item.get("lowSalary"), item.get("highSalary"),
        )
        salary_currency = item.get("currency") if (salary_min or salary_max) else None
        if isinstance(salary_currency, str):
            salary_currency = salary_currency.strip().upper() or None
            # The API uses "RS" / "RR" in a few rows; normalize the most
            # common alternates to the canonical ISO code.
            if salary_currency in {"RS", "RR", "RUPEES", "INR."}:
                salary_currency = "INR"
            if not (isinstance(salary_currency, str) and len(salary_currency) == 3):
                salary_currency = None

        description = _clean_description(item.get("description"))
        posted_at = _date_to_dt(item.get("postDate"))

        # Job type "On-site" / "Remote" / "Hybrid" is surfaced by the
        # API — promote "Remote" to ``is_remote=True``. We don't set
        # ``is_remote=False`` for on-site since the canonical model
        # only ever asserts True (the absence of a remote marker is
        # not evidence of on-site).
        job_type = item.get("jobType")
        is_remote = (
            True if isinstance(job_type, str) and "remote" in job_type.lower()
            else None
        )

        # Skills is a comma-joined string on the list endpoint. Split,
        # trim, drop empties.
        skills_raw = item.get("skills")
        skills: list[str] = []
        if isinstance(skills_raw, str):
            skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

        # Experience is a (from, to) range — surface the lower bound
        # as ``experience`` for canonical filtering, keep the raw range
        # in ``raw`` so consumers can render "5-8 years".
        exp_from = _safe_int(item.get("experienceFrom"))
        exp_to = _safe_int(item.get("experienceTo"))
        experience = exp_from if exp_from is not None and exp_from >= 0 else None

        raw: dict[str, Any] = {}
        if exp_from is not None and exp_from >= 0:
            raw["experience_from"] = exp_from
        if exp_to is not None and exp_to >= 0:
            raw["experience_to"] = exp_to
        if exp_from is not None and exp_to is not None and exp_from >= 0 and exp_to >= 0:
            raw["experience_label"] = (
                f"{exp_from}-{exp_to} years" if exp_from != exp_to
                else f"{exp_from} years"
            )
        if skills:
            raw["skills"] = skills[:30]
        if isinstance(job_type, str) and job_type.strip():
            raw["job_type"] = job_type.strip()
        job_function = item.get("jobFunction")
        if isinstance(job_function, str) and job_function.strip():
            raw["job_function"] = job_function.strip()
        expiry = item.get("expiryDate")
        if isinstance(expiry, str) and expiry.strip():
            raw["expiry_date"] = expiry.strip()

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.TIMESJOBS,
            ats_id=ats_id,
            location=location,
            country_iso=_infer_in_country_iso(location),
            language="en",
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience,
            department=raw.get("job_function"),
            commitment=raw.get("job_type"),
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


# A non-exhaustive set of major Indian metros + state names that
# unambiguously imply ``country_iso='IN'`` when they appear as the
# whole or majority of a TimesJobs ``location`` string. The list is
# deliberately conservative — for ambiguous / mixed locations
# ("Other City(s) in New York, Bengaluru") we leave ``country_iso``
# unset and the downstream LLM enrichment decides.
_INDIAN_CITIES = frozenset(
    {
        "ahmedabad", "bengaluru", "bangalore", "bhubaneswar", "chandigarh",
        "chennai", "coimbatore", "delhi", "new delhi", "faridabad",
        "ghaziabad", "gurgaon", "gurugram", "guwahati", "hubli",
        "hyderabad", "hyderabad/secunderabad", "indore", "jaipur",
        "jamshedpur", "kanpur", "kochi", "kolkata", "kozhikode",
        "lucknow", "ludhiana", "madurai", "mangalore", "mumbai",
        "mysore", "nagpur", "nashik", "navi mumbai", "noida",
        "patna", "pune", "raipur", "ranchi", "secunderabad",
        "surat", "thane", "thiruvananthapuram", "tiruchirapalli",
        "trichy", "trivandrum", "vadodara", "varanasi", "vijayawada",
        "visakhapatnam", "vizag",
    }
)

_INDIAN_REGIONS = frozenset({
    "andhra pradesh", "arunachal pradesh", "assam", "bihar",
    "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
    "jharkhand", "karnataka", "kerala", "madhya pradesh",
    "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "delhi", "india",
})


def _infer_in_country_iso(location: str | None) -> str | None:
    """Return ``"IN"`` when the location string is unambiguously
    Indian, otherwise ``None`` so LLM enrichment can derive it.

    TimesJobs is an Indian board but indexes global postings too —
    blindly stamping every row with ``IN`` would corrupt the country
    field for the ~30% non-IN inventory.
    """
    if not isinstance(location, str) or not location.strip():
        return None
    parts = [p.strip().lower() for p in location.split(",") if p.strip()]
    if not parts:
        return None
    if parts[-1] == "india":
        return "IN"
    if (
        parts[-1] == "in"
        and len(parts) > 1
        and all(
            part in _INDIAN_CITIES or part in _INDIAN_REGIONS
            for part in parts[:-1]
        )
    ):
        return "IN"
    if all(p in _INDIAN_CITIES or p in _INDIAN_REGIONS for p in parts):
        return "IN"
    return None


def _clean_location(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _clean_salary_range(
    low: object, high: object
) -> tuple[float | None, float | None]:
    lo = _to_positive_float(low)
    hi = _to_positive_float(high)
    return lo, hi


def _to_positive_float(value: object) -> float | None:
    if isinstance(value, bool):  # bool subclass of int — exclude explicitly
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _date_to_dt(value: object) -> datetime | None:
    """``postDate`` is an ISO date string (``YYYY-MM-DD``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Support both ``YYYY-MM-DD`` and the occasional full ISO timestamp.
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _clean_description(value: object) -> str | None:
    """Strip HTML, collapse whitespace, truncate to ~10kB."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10_000] or None
