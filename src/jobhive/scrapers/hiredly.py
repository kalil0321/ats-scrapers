"""Hiredly Malaysia (https://my.hiredly.com) — direct-employer jobs board.

Hiredly is a Malaysian direct-posting jobs platform — companies post
their own roles, it's not aggregated from LinkedIn / Indeed. The
``/jobs`` index page is a Next.js SSR app: the same job objects that
the browser renders are also served verbatim as JSON from the
``_next/data/{buildId}/jobs.json`` endpoint, which is what we hit.

The ``buildId`` is the Next.js per-deployment hash that appears both
inline on the HTML page and in the data URL. We discover it once by
fetching ``/jobs`` and grepping ``"buildId":"…"`` out of the HTML.
If a deploy happens mid-walk the data endpoint starts returning 404 —
we re-discover the build and resume from the same page.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / getonbrd / wanted pattern).
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

API_ROOT = "https://my.hiredly.com"
LISTING_PATH = "/jobs"
PER_PAGE = 30  # site renders 30 items per page; not configurable
DEFAULT_MAX_PAGES = 400  # ~12k jobs ceiling; site currently sits ~6k
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')


@ScraperRegistry.register(ATSType.HIREDLY)
class HiredlyScraper(BaseScraper):
    """Hiredly Malaysia (my.hiredly.com) — single-source scraper.

    ``company_slug`` is ignored. Pass anything (``"any"``, ``""``) —
    the scraper walks every job on the site until the page is empty.

    Knobs:
    - ``max_pages`` — pagination cap (default 400, ~12k jobs).
    """

    ats = ATSType.HIREDLY

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self._build_id: str | None = None

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> int:
            new = 0
            async with lock:
                for item in items:
                    job = self._parse(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new += 1
            return new

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            self._build_id = await self._discover_build_id(client, sem)
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 2:
                items = await self._fetch_page(client, sem, page=page)
                if not items:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                    await absorb(items)
                page += 1
        return jobs

    # --- build-id discovery -------------------------------------------------

    async def _discover_build_id(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> str:
        url = f"{API_ROOT}{LISTING_PATH}"
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers=_listing_headers(),
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Hiredly buildId discovery failed: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                match = _BUILD_ID_RE.search(response.text)
                if not match:
                    raise ScraperError(
                        "Hiredly: could not locate buildId in /jobs HTML"
                    )
                return match.group(1)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Hiredly /jobs returned {response.status_code} "
                        f"after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Hiredly /jobs returned {response.status_code}"
            )
        raise ScraperError(
            f"Hiredly buildId discovery exhausted retries: {last_exc}"
        )

    # --- listing pages ------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> list[dict[str, Any]]:
        # If the build was rotated mid-walk we re-discover it once and
        # retry the same page before treating the 404 as fatal.
        rediscovered = False
        for _ in range(2):
            payload = await self._fetch_data_page(
                client, sem, page=page,
            )
            if payload is _STALE_BUILD:
                if rediscovered:
                    raise ScraperError(
                        f"Hiredly: build rotated twice for page={page}"
                    )
                log.info(
                    "Hiredly: stale buildId=%s detected at page=%d, "
                    "re-discovering",
                    self._build_id, page,
                )
                self._build_id = await self._discover_build_id(client, sem)
                rediscovered = True
                continue
            return _extract_jobs(payload)
        return []

    async def _fetch_data_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any] | object:
        assert self._build_id is not None
        url = (
            f"{API_ROOT}/_next/data/{self._build_id}/jobs.json?page={page}"
        )
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers=_data_headers(),
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Hiredly fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Hiredly returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 404:
                # Stale buildId — caller re-discovers and retries.
                return _STALE_BUILD
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Hiredly returned {response.status_code} at "
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
                f"Hiredly returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"Hiredly exhausted retries at page={page}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        title = (item.get("title") or "").strip()
        if not raw_id or not title:
            return None
        ats_id = str(raw_id)
        slug = (item.get("slug") or "").strip()
        # Hiredly's public URL is ``/jobs/{slug}`` — the slug already
        # starts with ``jobs-malaysia-{company}-job-{role}``. Fall
        # back to the UUID-based path when the slug is missing.
        url = (
            f"{API_ROOT}/jobs/{slug}" if slug
            else f"{API_ROOT}/jobs/{ats_id}"
        )

        company_obj = item.get("company") or {}
        company_name = (company_obj.get("name") or "").strip() or "Unknown"

        salary_summary = (item.get("salary") or "").strip() or None
        sal_min, sal_max = _parse_salary_range(salary_summary)

        employment_type = _employment_type(item.get("jobType"))
        location = _format_location(item)

        raw: dict[str, Any] = {}
        category = item.get("category")
        if isinstance(category, str) and category.strip():
            raw["category"] = category.strip()
        skills_raw = item.get("skills") or []
        if isinstance(skills_raw, list):
            skills = [
                s.get("name").strip()
                for s in skills_raw
                if isinstance(s, dict)
                and isinstance(s.get("name"), str)
                and s["name"].strip()
            ]
            if skills:
                raw["skills"] = skills
        tracks_raw = item.get("tracks") or []
        if isinstance(tracks_raw, list):
            tracks = [
                t.get("title").strip()
                for t in tracks_raw
                if isinstance(t, dict)
                and isinstance(t.get("title"), str)
                and t["title"].strip()
            ]
            if tracks:
                raw["tracks"] = tracks
        career_level = item.get("careerLevel")
        if isinstance(career_level, str) and career_level.strip():
            raw["career_level"] = career_level.strip()
        if company_obj.get("slug"):
            raw["company_slug"] = company_obj["slug"]
        if company_obj.get("id"):
            raw["company_id"] = company_obj["id"]
        if item.get("gptSummary"):
            raw["gpt_summary"] = item["gptSummary"]
        if item.get("stateRegion"):
            raw["state_region"] = item["stateRegion"]

        return Job(
            url=url,
            title=title,
            company=company_name,
            ats_type=ATSType.HIREDLY,
            ats_id=ats_id,
            location=location,
            country_iso="MY",
            language="en",
            salary_summary=salary_summary,
            salary_currency="MYR" if salary_summary else None,
            salary_period="MONTH" if salary_summary else None,
            salary_min=sal_min,
            salary_max=sal_max,
            employment_type=employment_type,
            commitment=(item.get("jobType") or None),
            experience=_to_int(item.get("minYearsExperience")),
            posted_at=_parse_date(item.get("activeAt")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# Sentinel returned by ``_fetch_data_page`` on 404 (stale buildId).
_STALE_BUILD = object()


def _extract_jobs(payload: dict[str, Any] | object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    page_props = payload.get("pageProps") or {}
    jobs = page_props.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _listing_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }


def _data_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Referer": f"{API_ROOT}/jobs",
    }


def _employment_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "full_time": "FULL_TIME",
        "part_time": "PART_TIME",
        "contract": "CONTRACT",
        "internship": "INTERN",
        "intern": "INTERN",
        "temporary": "TEMPORARY",
    }.get(normalized)


def _format_location(item: dict[str, Any]) -> str | None:
    """Hiredly serves a verbose street address in ``location``; the
    ``stateRegion`` field is the human-friendly Malaysian state. Prefer
    the state when both are present (consumers care about ``Selangor``
    not the full Jalan address), but fall back to the longer string."""
    state = item.get("stateRegion")
    if isinstance(state, str) and state.strip():
        return state.strip()
    loc = item.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()
    return None


def _parse_salary_range(value: str | None) -> tuple[float | None, float | None]:
    """Hiredly's ``salary`` is plain ``"3000 - 4000"`` (MYR/month).
    Some postings only set the lower bound or leave it empty. Returns
    ``(None, None)`` when the string isn't a recognizable range."""
    if not value:
        return None, None
    match = re.match(
        r"\s*(\d+(?:[.,]\d+)?)\s*(?:-|–|to)\s*(\d+(?:[.,]\d+)?)\s*$",
        value,
    )
    if match:
        try:
            return (
                float(match.group(1).replace(",", "")),
                float(match.group(2).replace(",", "")),
            )
        except ValueError:
            return None, None
    single = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*$", value)
    if single:
        try:
            v = float(single.group(1).replace(",", ""))
            return v, v
        except ValueError:
            return None, None
    return None, None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Hiredly returns ISO-8601 with a +08:00 offset (Malaysia time).
    try:
        # Python's fromisoformat handles ``+08:00`` from 3.11 onward.
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    # Fallback: drop everything after the timezone if it's malformed.
    try:
        return datetime.fromisoformat(text.split("+")[0].split("Z")[0])
    except ValueError:
        return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None

