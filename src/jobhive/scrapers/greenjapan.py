"""Green Japan (https://www.green-japan.com) — Japanese tech jobs scraper.

Green is the largest direct-employer tech jobs board in Japan
(~29.6k active postings across ~4k companies, verified 2026-05-11).
The ``/search_key`` and ``/search`` SSR pages embed the same job
listing data inside the standard Next.js ``__NEXT_DATA__`` script at
``props.pageProps.defaultSearchJobOfferData`` — ``jobOffers`` (20 per
page) and ``totalJobOfferCount`` (the absolute total we paginate
against).

We hit the underlying ``_next/data/{buildId}/search.json?page=N`` JSON
endpoint after a one-shot HTML discovery for the ``buildId``. Falls
back to HTML scraping if the data endpoint 404's (stale buildId);
re-discovers and resumes from the same page.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / wanted pattern).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

API_ROOT = "https://www.green-japan.com"
LISTING_PATH = "/search_key"
PER_PAGE = 20  # site renders 20 items per page; not configurable
DEFAULT_MAX_PAGES = 2000  # ~40k jobs ceiling, site currently ~29.6k
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL,
)


@ScraperRegistry.register(ATSType.GREENJAPAN)
class GreenJapanScraper(BaseScraper):
    """Green Japan (green-japan.com) — single-source scraper.

    ``company_slug`` is ignored. Pass anything (``"any"``, ``""``) —
    the scraper walks every active posting up to ``totalJobOfferCount``.

    Knobs:
    - ``max_pages`` — pagination cap (default 2000, ~40k jobs).
    """

    ats = ATSType.GREENJAPAN

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
            # Discover buildId and grab page-1 data in the same HTML
            # fetch — saves one round-trip.
            self._build_id, first_items, total = (
                await self._discover_and_first_page(client, sem)
            )
            await absorb(first_items)
            if total is None:
                # Defensive: keep paging until empty if the total is
                # missing for some reason.
                page_count = self.max_pages
            else:
                page_count = min(
                    (total + PER_PAGE - 1) // PER_PAGE,
                    self.max_pages,
                )

            consecutive_empty = 0
            page = 2
            while page <= page_count and consecutive_empty < 2:
                items = await self._fetch_page(client, sem, page=page)
                if not items:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                    await absorb(items)
                page += 1
        return jobs

    # --- build-id + first-page discovery ----------------------------------

    async def _discover_and_first_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> tuple[str, list[dict[str, Any]], int | None]:
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
                            f"Green Japan discovery failed: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                text = response.text
                build_match = _BUILD_ID_RE.search(text)
                if not build_match:
                    raise ScraperError(
                        "Green Japan: could not locate buildId in HTML"
                    )
                build_id = build_match.group(1)
                items, total = _parse_next_data(text)
                return build_id, items, total
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Green Japan /search_key returned "
                        f"{response.status_code} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Green Japan /search_key returned {response.status_code}"
            )
        raise ScraperError(
            f"Green Japan discovery exhausted retries: {last_exc}"
        )

    async def _rediscover_build_id(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> str:
        build_id, _items, _total = await self._discover_and_first_page(
            client, sem,
        )
        return build_id

    # --- listing pages ----------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> list[dict[str, Any]]:
        rediscovered = False
        for _ in range(2):
            payload = await self._fetch_data_page(
                client, sem, page=page,
            )
            if payload is _STALE_BUILD:
                if rediscovered:
                    raise ScraperError(
                        f"Green Japan: build rotated twice for page={page}"
                    )
                log.info(
                    "Green Japan: stale buildId=%s at page=%d, "
                    "re-discovering",
                    self._build_id, page,
                )
                self._build_id = await self._rediscover_build_id(
                    client, sem,
                )
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
            f"{API_ROOT}/_next/data/{self._build_id}"
            f"/search.json?page={page}"
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
                            f"Green Japan fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Green Japan returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 404:
                return _STALE_BUILD
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Green Japan returned {response.status_code} at "
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
                f"Green Japan returned {response.status_code} at "
                f"page={page}"
            )
        raise ScraperError(
            f"Green Japan exhausted retries at page={page}: {last_exc}"
        )

    # --- parsing ----------------------------------------------------------

    def _parse(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id)
        # Green uses ``name`` for the role title; ``title`` is a marketing
        # tagline ("賞与実績…"). Fall back to ``title`` if ``name`` is
        # missing.
        title = (item.get("name") or item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        company_obj = item.get("company") or {}
        company_name = (company_obj.get("name") or "").strip() or "Unknown"

        # Job URL: per the listing payload, ``jobOfferUrl`` is the
        # canonical ``/company/{cid}/job/{jid}`` path. Fall back to the
        # bare ``/job/{id}`` form when missing.
        job_url_path = (item.get("jobOfferUrl") or "").strip()
        if job_url_path.startswith("/"):
            url = f"{API_ROOT}{job_url_path}"
        else:
            url = f"{API_ROOT}/job/{ats_id}"

        salary_summary = (item.get("salary") or "").strip() or None
        sal_min, sal_max = _parse_jpy_salary(salary_summary)

        raw: dict[str, Any] = {}
        if isinstance(item.get("areaName"), str) and item["areaName"].strip():
            raw["area"] = item["areaName"].strip()
        skill_names = item.get("skillNames")
        if isinstance(skill_names, list) and skill_names:
            raw["skills"] = [
                s for s in skill_names if isinstance(s, str) and s
            ]
        tag_names = item.get("tagNames")
        if isinstance(tag_names, list) and tag_names:
            raw["tags"] = [
                t for t in tag_names if isinstance(t, str) and t
            ]
        client_business = item.get("clientBusiness")
        if isinstance(client_business, dict):
            cb_name = client_business.get("name")
            if isinstance(cb_name, str) and cb_name.strip():
                raw["industry"] = cb_name.strip()
        if company_obj.get("id") is not None:
            raw["company_id"] = company_obj["id"]
        if isinstance(company_obj.get("title"), str) and company_obj["title"].strip():
            raw["company_tagline"] = company_obj["title"].strip()
        # Keep the marketing tagline distinct from the role name so
        # downstream consumers can still surface it.
        if isinstance(item.get("title"), str) and item["title"].strip():
            tagline = item["title"].strip()
            if tagline != title:
                raw["headline"] = tagline

        return Job(
            url=url,
            title=title,
            company=company_name,
            ats_type=ATSType.GREENJAPAN,
            ats_id=ats_id,
            location=_format_location(item.get("areaName")),
            country_iso="JP",
            region="Asia",
            language="ja",
            salary_summary=salary_summary,
            salary_currency="JPY" if salary_summary else None,
            salary_period="YEAR" if salary_summary else None,
            salary_min=sal_min,
            salary_max=sal_max,
            posted_at=_parse_unix_timestamp(
                item.get("jobOfferUpdatedAtTimestamp")
            ),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


_STALE_BUILD = object()


def _extract_jobs(payload: dict[str, Any] | object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    page_props = payload.get("pageProps") or {}
    dso = page_props.get("defaultSearchJobOfferData") or {}
    items = dso.get("jobOffers")
    return items if isinstance(items, list) else []


def _parse_next_data(html: str) -> tuple[list[dict[str, Any]], int | None]:
    """Pull the embedded ``__NEXT_DATA__`` JSON out of an HTML page
    and return the (jobs, totalCount) tuple from it."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return [], None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], None
    if not isinstance(payload, dict):
        return [], None
    items = _extract_jobs(payload.get("props") or {})
    dso = (
        ((payload.get("props") or {}).get("pageProps") or {})
        .get("defaultSearchJobOfferData") or {}
    )
    total = dso.get("totalJobOfferCount")
    if not isinstance(total, int) or total < 0:
        total = None
    return items, total


def _listing_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.9",
    }


def _data_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "ja,en;q=0.9",
        "Referer": f"{API_ROOT}{LISTING_PATH}",
    }


def _format_location(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return None
    return ", ".join(parts)


# Match common Japanese salary formats:
#   "410万円〜800万円"       → 4.1M - 8M JPY (both bounds carry the unit)
#   "300〜600万円"           → 3M - 6M JPY (range with one shared 万円 suffix)
#   "500万円以上"            → 5M JPY, min only
#   "500万円"                 → 5M JPY, single value
_RANGE_BOTH_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*万円\s*[〜~\-–]\s*(\d+(?:[.,]\d+)?)\s*万円",
)
_RANGE_SHARED_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[〜~\-–]\s*(\d+(?:[.,]\d+)?)\s*万円",
)
_SINGLE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*万円")


def _parse_jpy_salary(value: str | None) -> tuple[float | None, float | None]:
    """Green Japan reports salary as Japanese 万円 (10,000 JPY) ranges.

    Returns ``(min_jpy, max_jpy)`` with both bounds in absolute JPY,
    or ``(None, None)`` if no recognizable range is present."""
    if not value:
        return None, None
    for regex in (_RANGE_BOTH_UNIT_RE, _RANGE_SHARED_UNIT_RE):
        match = regex.search(value)
        if match:
            try:
                low = float(match.group(1).replace(",", "")) * 10_000
                high = float(match.group(2).replace(",", "")) * 10_000
            except ValueError:
                continue
            return low, high
    match = _SINGLE_RE.search(value)
    if match:
        try:
            low = float(match.group(1).replace(",", "")) * 10_000
        except ValueError:
            return None, None
        return low, None
    return None, None


def _parse_unix_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None
