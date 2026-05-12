"""Shine.com (https://www.shine.com) — India-focused jobs board scraper.

Shine.com is one of India's top general-purpose jobs boards (alongside
Naukri, Foundit and TimesJobs). It carries ~240k live postings across
every sector — IT, BFSI, BPO, manufacturing, healthcare, etc. — with
companies (employers and recruiting agencies) posting directly through
Shine's recruiter product.

The site is a Next.js SPA. Each ``/job-search/all-jobs[-N]`` listing page
ships a ``<script id="__NEXT_DATA__">`` block with the fully-populated
``jsrp.searchresult.data.results`` array — no separate JSON API hop, no
CSR-only fetch. Each result entry carries enough fields that we don't
need per-job detail requests:

  id        → posting ID (``jSlug`` embeds it too)
  jJT       → title
  jCName    → company name
  jSlug     → URL slug (``{title-slug}/{company-slug}/{id}``)
  jLoc      → list of locations (``["All India", "Bangalore"]``)
  jJD       → description text
  jExp      → experience range (``"3 to 7 Yrs"``)
  jPDate    → posted-at ISO timestamp
  jKwd      → comma-separated skills/keywords
  jInd      → industry
  jSal      → salary string (almost always ``"[Salary Hidden]"``)
  jJobType  → employment type code (1=Full-time, in observed data)

Pagination: ``/job-search/all-jobs`` is page 1, ``/job-search/all-jobs-2``
is page 2, etc. The payload's ``num_pages`` field reports the total
(~12k pages × 20 = ~240k jobs). We cap at ``max_pages`` to keep a single
sweep tractable; the default (300) yields ~6,000 most-recent jobs.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / wanted / getonbrd pattern). Output rows
carry the publishing employer's name as ``company`` so cross-ATS dedup
still works.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.shine.com"
LISTING_PATH = "/job-search/all-jobs"
DEFAULT_MAX_PAGES = 300  # ~6,000 most-recent jobs.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
    re.DOTALL,
)
_EXPERIENCE_RE = re.compile(
    r"(?P<lo>\d+)\s*(?:to|-)\s*(?P<hi>\d+)\s*(?:Yr|Year)",
    re.IGNORECASE,
)
_SINGLE_EXP_RE = re.compile(r"(\d+)\s*(?:Yr|Year)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# Shine ``jJobType`` code → canonical EmploymentType. The observed live
# data only ever surfaces ``1`` (Full-Time); the rest are best-effort
# guesses from Shine's recruiter docs and are safe defaults if/when
# they appear.
_JOBTYPE_MAP: dict[int, str] = {
    1: "FULL_TIME",
    2: "PART_TIME",
    3: "CONTRACT",
    4: "INTERN",
    5: "TEMPORARY",
}


@ScraperRegistry.register(ATSType.SHINE)
class ShineScraper(BaseScraper):
    """Shine.com (shine.com) — India-focused multi-sector jobs board.

    Single-source: ``company_slug`` is ignored. Pass anything (``"any"``,
    ``""``) — the scraper paginates the full ``all-jobs`` listing.

    Knobs:
    - ``max_pages`` — pagination cap (default 300 → ~6,000 jobs). Shine's
      ``num_pages`` is ~12k so a full sweep is impractical for a single
      run; cap small and re-run for newest postings.
    """

    ats = ATSType.SHINE

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
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> int:
            new = 0
            async with lock:
                for j in items:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)
                    new += 1
            return new

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Probe page 1 — learn ``num_pages`` so we don't blindly hit
            # the per-instance cap when the listing is shorter.
            first_text = await self._fetch_listing_text(client, sem, page=1)
            first_payload = self._extract_next_data(first_text)
            first_data = self._search_data(first_payload)
            num_pages = int(first_data.get("num_pages") or 1)
            await absorb(list(self._parse_results(first_data)))

            last_page = min(num_pages, self.max_pages)
            if last_page <= 1:
                return jobs

            consecutive_empty = 0

            async def one(page: int) -> None:
                nonlocal consecutive_empty
                try:
                    text = await self._fetch_listing_text(client, sem, page=page)
                except ScraperError as exc:
                    # Deep pagination can hit transient blocks; keep what
                    # we've already gathered rather than blow up.
                    log.warning(
                        "Shine: stopping at page %d (%s); keeping %d jobs",
                        page, exc, len(jobs),
                    )
                    return
                payload = self._extract_next_data(text)
                data = self._search_data(payload)
                new = await absorb(list(self._parse_results(data)))
                if new == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0

            for page in range(2, last_page + 1):
                if consecutive_empty >= 3:
                    break
                await one(page)
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_listing_text(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> str:
        suffix = "" if page == 1 else f"-{page}"
        url = f"{API_ROOT}{LISTING_PATH}{suffix}"
        return await self._request_html(client, sem, url)

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
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,*/*",
                            "Accept-Language": "en-IN,en;q=0.9",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Shine fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # Page past the end — caller treats as "no data".
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Shine returned {response.status_code} for {url} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Shine returned {response.status_code} for {url}"
            )
        raise ScraperError(f"Shine exhausted retries for {url}: {last_exc}")

    # --- parsing ------------------------------------------------------------

    def _extract_next_data(self, text: str) -> dict[str, Any]:
        if not text:
            return {}
        match = _NEXT_DATA_RE.search(text)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"Shine __NEXT_DATA__ payload was not valid JSON: {exc}"
            ) from exc

    def _search_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Drill down to ``jsrp.searchresult.data`` defensively — the
        Next.js page-prop tree is hand-wired, so any of the intermediate
        keys can in theory be missing on edge cases."""
        try:
            return (
                payload["props"]["pageProps"]
                ["initialState"]["jsrp"]["searchresult"]["data"]
            ) or {}
        except (KeyError, TypeError):
            return {}

    def _parse_results(self, data: dict[str, Any]):
        for item in data.get("results") or []:
            job = self._parse_item(item)
            if job is not None:
                yield job

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id).strip()
        title = _clean_text(item.get("jJT") or "")
        if not ats_id or not title:
            return None

        company = _clean_text(item.get("jCName") or "") or "Unknown"
        slug = (item.get("jSlug") or "").strip().strip("/")
        url = (
            f"{API_ROOT}/jobs/{slug}"
            if slug
            else f"{API_ROOT}/jobs/{ats_id}"
        )

        location, country_iso = _format_location(item.get("jLoc"))

        experience_min, experience_max = _parse_experience(
            item.get("jExp") or ""
        )

        description = _clean_text(item.get("jJD") or "")[:10_000] or None

        posted_at = _parse_iso(item.get("jPDate"))

        employment_type = _JOBTYPE_MAP.get(_to_int(item.get("jJobType")))

        salary_summary = _clean_salary(item.get("jSal"))

        skills = _parse_skills(item.get("jKwd"))

        raw: dict[str, Any] = {}
        if experience_min is not None:
            raw["experience_min"] = experience_min
        if experience_max is not None:
            raw["experience_max"] = experience_max
        if skills:
            raw["skills"] = skills[:30]
        industry = _clean_text(item.get("jInd") or "")
        if industry:
            raw["industry"] = industry

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.SHINE,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            language="en",
            experience=experience_min,
            employment_type=employment_type,
            salary_summary=salary_summary,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_salary(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    # Shine renders hidden ranges as ``"[Salary Hidden]"``. Treat as no
    # signal — there's no number to extract and the literal isn't useful
    # downstream.
    if "salary hidden" in cleaned.lower():
        return None
    return cleaned


def _format_location(value: object) -> tuple[str | None, str | None]:
    """Shine's ``jLoc`` is a list of city strings (``["All India"]``,
    ``["Bangalore", "Pune"]``). Most postings live in India; a handful
    list Gulf / SEA cities. Return the comma-joined display + an ISO
    country guess (defaults to ``IN`` for India-specific markers, ``None``
    when the location is ambiguous so LLM enrichment fills it in).
    """
    if not isinstance(value, list):
        return None, "IN" if isinstance(value, str) and value.strip() else None
    parts = [p.strip() for p in value if isinstance(p, str) and p.strip()]
    if not parts:
        return None, None
    display = ", ".join(parts[:5])
    lowered = display.lower()
    # "All India" / "Bangalore" / "Mumbai" etc. → India. Outside-India
    # cities (Dubai, Singapore, …) are rare and ambiguous; defer to
    # the downstream country-from-location enrichment by returning None.
    india_markers = (
        "india", "bangalore", "bengaluru", "mumbai", "pune", "chennai",
        "hyderabad", "delhi", "noida", "gurgaon", "gurugram", "kolkata",
        "ahmedabad", "kochi", "thiruvananthapuram", "jaipur", "lucknow",
        "indore", "chandigarh", "coimbatore", "nagpur", "bhubaneswar",
    )
    if any(m in lowered for m in india_markers):
        return display, "IN"
    return display, None


def _parse_experience(raw: str) -> tuple[int | None, int | None]:
    """``"3 to 7 Yrs"`` → ``(3, 7)``; ``"5 Yrs"`` → ``(5, 5)``."""
    if not raw:
        return None, None
    m = _EXPERIENCE_RE.search(raw)
    if m:
        try:
            return int(m.group("lo")), int(m.group("hi"))
        except ValueError:
            return None, None
    m = _SINGLE_EXP_RE.search(raw)
    if m:
        try:
            v = int(m.group(1))
        except ValueError:
            return None, None
        return v, v
    return None, None


def _parse_skills(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    parts = re.split(r"[,;]", raw)
    return [p.strip() for p in parts if p and p.strip()]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _to_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
