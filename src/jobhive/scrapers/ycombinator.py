"""Y Combinator startup jobs (https://www.ycombinator.com/jobs).

Scrapes every YC-portfolio company that is currently hiring. The
``ycombinator.com/jobs`` page only shows ~20 highlighted postings,
but each YC company's ``/companies/{slug}`` page has the full job
list embedded as JSON (entity-encoded inside the HTML). We discover
the universe of currently-hiring companies through the public
``api.ycombinator.com/v0.1/companies?isHiring=true`` paginated API,
then fetch each company's page and pull its ``jobPostings`` JSON.

Direct YC postings only — companies use Y Combinator's own platform
(workatastartup), not LinkedIn / Indeed syndication.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_HOST = "https://api.ycombinator.com"
WEB_HOST = "https://www.ycombinator.com"
COMPANIES_API = f"{API_HOST}/v0.1/companies"
COMPANY_PAGE_TEMPLATE = f"{WEB_HOST}/companies/{{slug}}"
PER_PAGE = 25  # YC's company API ignores ``per_page``; pagination is
                # always 25 / page so we just walk pages.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5


@ScraperRegistry.register(ATSType.YCOMBINATOR)
class YCombinatorScraper(BaseScraper):
    """Y Combinator startup jobs.

    Single-source: ``company_slug`` is ignored. Pass anything (``"any"``,
    ``""``).

    Knobs:
    - ``max_company_pages`` — pagination cap on the hiring-companies
      API (default 50, ~1,250 companies; current live count is ~260).
    """

    ats = ATSType.YCOMBINATOR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_company_pages: int = 50,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_company_pages = max_company_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for j in items:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            slugs = await self._list_hiring_company_slugs(client, sem)

            async def per_company(slug: str) -> None:
                co_jobs = await self._fetch_company_jobs(client, sem, slug)
                await absorb(co_jobs)

            await asyncio.gather(*(per_company(s) for s in slugs))
        return jobs

    # --- discovery ---------------------------------------------------------

    async def _list_hiring_company_slugs(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> list[str]:
        """Walk the YC public companies API with ``isHiring=true``,
        return every distinct company slug across all pages."""
        slugs: list[str] = []
        seen_slugs: set[str] = set()
        page = 1
        while page <= self.max_company_pages:
            params = {"isHiring": "true", "page": page}
            payload = await self._request_json(client, sem, COMPANIES_API, params=params)
            companies = payload.get("companies") or []
            if not companies:
                break
            new = 0
            for c in companies:
                slug = (c.get("slug") or "").strip()
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    slugs.append(slug)
                    new += 1
            total_pages = int(payload.get("totalPages") or 0)
            next_page_url = payload.get("nextPage")
            # Fallback: stop when a page yields nothing new (the API
            # doesn't always set totalPages / nextPage).
            if not next_page_url and not total_pages and new == 0:
                break
            if total_pages and page >= total_pages:
                break
            page += 1
        return slugs

    async def _fetch_company_jobs(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slug: str,
    ) -> list[Job]:
        """Fetch the YC company page, extract its embedded
        ``jobPostings`` JSON, and parse out Job rows."""
        url = COMPANY_PAGE_TEMPLATE.format(slug=slug)
        text = await self._request_html(client, sem, url)
        # Decoded once — entire page so the array search works on
        # human-readable JSON.
        decoded = html.unescape(text)
        raw_array = _extract_balanced_array(decoded, '"jobPostings":')
        if raw_array is None:
            return []
        try:
            postings = json.loads(raw_array)
        except json.JSONDecodeError:
            return []
        return [
            j for j in (self._parse_posting(p, slug=slug) for p in postings)
            if j is not None
        ]

    def _parse_posting(self, item: dict[str, Any], *, slug: str) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        url_path = (item.get("url") or "").strip()
        if not ats_id or not title or not url_path:
            return None

        job_url = url_path if url_path.startswith("http") else f"{WEB_HOST}{url_path}"
        company = (item.get("companyName") or slug).strip()
        location = (item.get("location") or "").strip() or None

        salary_min, salary_max, salary_currency = _parse_salary_range(
            item.get("salaryRange")
        )

        # ``minExperience`` is "3+ years" / "5+ years" / "0 years" /
        # ""; pull the leading number when present.
        experience = _parse_min_experience(item.get("minExperience"))

        # ``type`` is "Full-time" / "Part-time" / "Internship" / "Contract"
        employment_type = _employment_from_type(item.get("type"))

        # ``createdAt`` is relative ("16 days", "1 day", "2 hours") —
        # convert to an approximate absolute datetime so dataset
        # consumers have a recency signal even though it's fuzzy.
        posted_at = _parse_relative_age(item.get("createdAt"))

        apply_raw = item.get("applyUrl")
        apply_url: str | None = None
        if isinstance(apply_raw, str) and apply_raw.startswith(("http://", "https://")):
            apply_url = apply_raw

        raw: dict[str, Any] = {}
        for key in (
            "role", "prettyRole", "roleSpecificType", "minSchoolYear",
            "visa", "skills", "equityRange", "companyBatchName",
            "lastActive", "askUs",
        ):
            v = item.get(key)
            if v not in (None, "", []):
                raw[key] = v

        description = _compose_description(item)

        return Job(
            url=job_url,
            title=title,
            company=company,
            ats_type=ATSType.YCOMBINATOR,
            ats_id=ats_id,
            location=location,
            salary_currency=salary_currency,
            salary_period="YEAR" if salary_currency else None,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience,
            employment_type=employment_type,
            commitment=item.get("type") if isinstance(item.get("type"), str) else None,
            department=(
                item.get("prettyRole") if isinstance(item.get("prettyRole"), str) else None
            ),
            apply_url=apply_url,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )

    # --- HTTP -------------------------------------------------------------

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, params=params, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"YC api fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"YC api returned non-JSON for {url}: {exc}"
                    ) from exc
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"YC api returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"YC api returned {response.status_code} for {url}"
            )
        raise ScraperError(f"YC api exhausted retries for {url}: {last_exc}")

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "text/html,*/*",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"YC company page fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # Company page might have been removed since we discovered
                # them; treat as 'no jobs' rather than crashing the run.
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"YC company returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"YC company returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"YC company exhausted retries for {url}: {last_exc}"
        )


# --- module helpers ---------------------------------------------------------


def _extract_balanced_array(text: str, marker: str) -> str | None:
    """Find ``marker`` in ``text`` and return the JSON array that
    immediately follows, scanning brackets to keep the slice balanced.
    Returns ``None`` if the marker is missing or the array is malformed.
    """
    idx = text.find(marker)
    if idx < 0:
        return None
    # Find the opening bracket after the marker.
    bracket = text.find("[", idx + len(marker))
    if bracket < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(bracket, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[bracket:i + 1]
    return None


_SALARY_RANGE_RE = re.compile(
    r"\$(\d+(?:\.\d+)?)\s*([KMm])?\s*(?:-|–|to)\s*\$?(\d+(?:\.\d+)?)\s*([KMm])?"
)
_SINGLE_SALARY_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*([KMm])?")


def _parse_salary_range(
    raw: object,
) -> tuple[float | None, float | None, str | None]:
    """YC's ``salaryRange`` is a free-text string: ``$180K - $250K``,
    ``$120K``, ``Equity only``, ``""``. Convert when numeric, else
    return ``(None, None, None)``."""
    if not isinstance(raw, str) or not raw.strip():
        return None, None, None
    m = _SALARY_RANGE_RE.search(raw)
    if m:
        lo = _scale_amount(m.group(1), m.group(2))
        hi = _scale_amount(m.group(3), m.group(4) or m.group(2))
        if lo or hi:
            return lo, hi, "USD"
    m = _SINGLE_SALARY_RE.search(raw)
    if m:
        amt = _scale_amount(m.group(1), m.group(2))
        if amt:
            return amt, amt, "USD"
    return None, None, None


def _scale_amount(num: str, suffix: str | None) -> float | None:
    try:
        v = float(num)
    except (TypeError, ValueError):
        return None
    if not suffix:
        return v
    s = suffix.lower()
    if s == "k":
        return v * 1_000
    if s == "m":
        return v * 1_000_000
    return v


_EXPERIENCE_RE = re.compile(r"^\s*(\d+)")


def _parse_min_experience(raw: object) -> int | None:
    if not isinstance(raw, str):
        return None
    m = _EXPERIENCE_RE.match(raw)
    return int(m.group(1)) if m else None


_TYPE_TO_EMPLOYMENT: dict[str, str] = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "internship": "INTERN",
    "intern": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "TEMPORARY",
}


def _employment_from_type(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return _TYPE_TO_EMPLOYMENT.get(raw.lower())


def _compose_description(item: dict[str, Any]) -> str | None:
    """Build a plain-text body from YC's embedded posting fields."""
    parts: list[str] = []
    for key in (
        "description",
        "descriptionPlain",
        "aboutRole",
        "responsibilities",
        "requirements",
        "companyOneLiner",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_clean_description_text(value))
        elif isinstance(value, list):
            cleaned = [
                _clean_description_text(v)
                for v in value
                if isinstance(v, str) and v.strip()
            ]
            if cleaned:
                parts.append("\n".join(cleaned))
    text = "\n\n".join(p for p in parts if p).strip()
    return text[:25_000] or None


def _clean_description_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


_RELATIVE_RE = re.compile(
    r"^\s*(\d+)\s*(minute|hour|day|week|month|year)s?\s*$",
    re.IGNORECASE,
)
_RELATIVE_TO_SECONDS = {
    "minute": 60,
    "hour": 60 * 60,
    "day": 60 * 60 * 24,
    "week": 60 * 60 * 24 * 7,
    "month": 60 * 60 * 24 * 30,  # approximate
    "year": 60 * 60 * 24 * 365,  # approximate
}


def _parse_relative_age(raw: object) -> datetime | None:
    """YC ``createdAt`` is a relative label ("16 days", "1 day", "2
    hours"). Subtract the duration from now to produce an
    approximate absolute datetime — better than None for a recency
    signal, but consumers should treat it as ±1 day on the day-scale
    cases."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    m = _RELATIVE_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    delta = n * _RELATIVE_TO_SECONDS.get(unit, 0)
    if delta <= 0:
        return None
    from datetime import timedelta
    return datetime.now() - timedelta(seconds=delta)
