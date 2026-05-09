"""jobs.ch — Switzerland's largest direct-posting job board (~50k active).

Companies pay to list on jobs.ch — postings are not syndicated from
LinkedIn / Indeed. Coverage spans all of Switzerland (DE-CH, FR-CH,
IT-CH, EN) across every sector (the API doesn't restrict to tech).
The May 2026 audit had Switzerland at 0.2% of the dataset; this is
roughly a 25× lift.

Public REST API at ``https://www.jobs.ch/api/v1/public/search`` — no
auth, no key. Pagination is ``?start=N&rows=20`` (rows hard-capped
at 20; >20 → 422). Each entry has ``company_name`` embedded so no
separate company-resolution fetch is needed. The detail-page URL
template is in ``_links.detail_*`` (German is the canonical default).

The API geo-fences off datacenter IPs — bare httpx from a Hetzner /
DigitalOcean / AWS machine returns 403 with a 919-byte block page
(verified 2026-05-09 from Hetzner). The scraper tries direct first
and, on 403, falls back to a residential proxy pulled from the
``PROXY`` env var (Evomi 4-colon shape ``http://host:port:user:pass``,
matching the Tesla / Meta pattern). With the proxy active, the same
endpoint returns 200 + ~200 KB HTML and the public REST API works.
When ``PROXY`` is not set the scraper raises a clear error rather
than silently 0-scraping.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://www.jobs.ch/api/v1/public/search"
PER_PAGE = 20  # API hard-caps ``rows`` at 20 (>20 → 422).
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
# Default cap on total pages to fetch — 2,500 pages × 20 jobs = full
# 50k inventory. Set lower via ``max_pages`` for incremental runs.
DEFAULT_MAX_PAGES = 2500


class _BlockedError(Exception):
    """Internal marker — the API returned 403, retry the whole fetch
    via the residential-proxy fallback. Not raised at the public
    boundary."""


@ScraperRegistry.register(ATSType.JOBSCH)
class JobsChScraper(BaseScraper):
    """jobs.ch (Switzerland) — direct-posting board.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap (default 2,500, ~50k jobs).
    """

    ats = ATSType.JOBSCH

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
        # Try direct first; on 403 from the datacenter-blocked API,
        # restart the whole fetch through the Evomi residential proxy.
        try:
            return await self._run_fetch(proxy_url=None)
        except _BlockedError:
            pass

        proxy_url = _evomi_proxy_url_from_env()
        if proxy_url is None:
            raise ScraperError(
                "jobs.ch returned 403 (likely datacenter IP block) and "
                "no PROXY env var is set. Set PROXY=http://host:port:user:pass "
                "to a residential proxy (Evomi or similar) to enable the "
                "fallback path."
            )
        log.info(
            "jobs.ch: direct request 403'd — retrying via PROXY "
            "residential fallback."
        )
        return await self._run_fetch(proxy_url=proxy_url)

    async def _run_fetch(self, *, proxy_url: str | None) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        # Track whether we've already escalated to the proxy. After
        # that, any further 403 mid-pagination is treated as a
        # rate-limit / regional dropout — we keep what we have and
        # stop, rather than re-raising and throwing away the slice.
        already_in_proxy_mode = proxy_url is not None

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if proxy_url is not None:
            client_kwargs["proxy"] = proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # First request to learn the real total. A 403 here on the
            # first page (no proxy yet) escalates so the caller can
            # retry through the residential proxy fallback.
            first = await self._fetch_page(client, sem, start=0)
            total = int(first.get("total_hits") or 0)
            await absorb(first.get("documents") or [])

            if total <= PER_PAGE:
                return jobs

            page_count = min(
                (total + PER_PAGE - 1) // PER_PAGE, self.max_pages
            )
            offsets = [PER_PAGE * i for i in range(1, page_count)]

            async def one(offset: int) -> None:
                try:
                    payload = await self._fetch_page(client, sem, start=offset)
                except _BlockedError:
                    if not already_in_proxy_mode:
                        raise  # let _fetch_async escalate to proxy
                    # Already on the proxy — different node may have
                    # hit a per-IP rate limit. Drop this page silently
                    # so partial pagination returns what we have.
                    return
                await absorb(payload.get("documents") or [])

            await asyncio.gather(*(one(o) for o in offsets))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        start: int,
    ) -> dict[str, Any]:
        params = {"start": start, "rows": PER_PAGE}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"jobs.ch fetch failed at start={start}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"jobs.ch returned non-JSON at start={start}: {exc}"
                    ) from exc
            if response.status_code == 403:
                # Datacenter IP block — escalate to ``_fetch_async`` so
                # it can retry the whole fetch through the residential
                # proxy. Don't burn retries here.
                raise _BlockedError(
                    f"jobs.ch returned 403 at start={start}"
                )
            if response.status_code == 422:
                # Past the search-engine cap (rare; API caps deep
                # pagination differently per query). Treat as exhausted.
                return {"documents": [], "total_hits": 0}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"jobs.ch returned {response.status_code} at "
                        f"start={start} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"jobs.ch returned {response.status_code} at start={start}"
            )
        raise ScraperError(
            f"jobs.ch exhausted retries at start={start}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("job_id") or "")
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not ats_id or not title:
            return None

        url = _detail_url(item, ats_id)

        # ``place`` is the city name; ``regions`` is a numeric path
        # (cantons + sub-regions) we don't have a name table for. The
        # city is enough for downstream geo-search.
        place = (item.get("place") or "").strip() or None
        location = f"{place}, Switzerland" if place else "Switzerland"

        # employment_grades is a list like [100] (% time). When the
        # only value is below 100 the role is part-time; when 100 it's
        # full-time; mixed lists indicate flexibility.
        grades = item.get("employment_grades") or []
        is_full_time = grades == [100]
        employment_type = "FULL_TIME" if is_full_time else (
            "PART_TIME" if grades and all(g < 100 for g in grades) else None
        )

        posted_at = _parse_iso(
            item.get("publication_date") or item.get("initial_publication_date")
        )

        raw: dict[str, Any] = {}
        if grades:
            raw["employment_grades"] = grades
        languages = [
            entry.get("language") for entry in (item.get("language_skills") or [])
            if isinstance(entry, dict) and entry.get("language")
        ]
        if languages:
            raw["languages"] = languages
        if item.get("company_id"):
            raw["company_id"] = str(item["company_id"])
        if item.get("company_segmentation"):
            raw["company_segmentation"] = item["company_segmentation"]

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.JOBSCH,
            ats_id=ats_id,
            location=location,
            employment_type=employment_type,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _detail_url(item: dict[str, Any], job_id: str) -> str:
    """Prefer ``_links.detail_{lang}.href`` when present (jobs.ch ships
    a localized detail URL per row), else fall back to the documented
    canonical English URL pattern.
    """
    links = item.get("_links") or {}
    if isinstance(links, dict):
        for key in ("detail_en", "detail_de", "detail_fr", "detail_it"):
            entry = links.get(key)
            if isinstance(entry, dict):
                href = entry.get("href")
                if isinstance(href, str) and href:
                    return href
    return f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _evomi_proxy_url_from_env() -> str | None:
    """Parse the ``PROXY`` env var into an httpx-compatible proxy URL.

    Evomi ships ``PROXY`` in the 4-colon
    ``http://host:port:user:pass`` shape (same shape the
    ``_browserbase`` helper consumes for patchright). We rebuild it
    into the standard ``http://user:pass@host:port`` form that httpx
    accepts. Returns ``None`` when no env var is set so the caller can
    surface a clear error instead of silently no-op'ing.
    """
    raw = os.getenv("PROXY")
    if not raw:
        return None
    rest = raw.replace("http://", "").replace("https://", "")
    parts = rest.split(":")
    if len(parts) != 4:
        log.warning(
            "PROXY env var doesn't match host:port:user:pass shape; "
            "skipping jobs.ch fallback."
        )
        return None
    host, port, user, password = parts
    return f"http://{user}:{password}@{host}:{port}"
