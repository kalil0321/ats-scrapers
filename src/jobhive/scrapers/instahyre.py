"""Instahyre — India-focused direct employer hiring platform.

Two-step **JSON-only** flow (no HTML job pages):

1. Paginate ``GET /api/v1/job_search`` for listing objects.
2. Best-effort ``GET /api/v1/job_search/{id}`` per job to re-validate the
   payload and refresh the composed description. Instahyre's public API
   does not expose a separate long-form JD field; we build plain text
   from structured API fields (title, location, keywords, employer
   metadata).

Detail failures keep listing-derived fields — same pattern as BambooHR /
Rippling enrichment.
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

API_URL = "https://www.instahyre.com/api/v1/job_search"
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 100
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 8


def _request_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.instahyre.com/search-jobs/",
    }


@ScraperRegistry.register(ATSType.INSTAHYRE)
class InstahyreScraper(BaseScraper):
    """Instahyre India tech jobs scraper."""

    ats = ATSType.INSTAHYRE

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        detail_concurrency: int = DETAIL_CONCURRENCY,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.page_size = page_size
        self.max_pages = max_pages
        self.detail_concurrency = detail_concurrency

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page in range(self.max_pages):
                try:
                    payload = await self._fetch_page(client, offset=page * self.page_size)
                except ScraperError:
                    if page == 0:
                        raise
                    break
                if not isinstance(payload, dict):
                    raise ScraperError(
                        "Instahyre job_search returned non-object JSON "
                        f"at offset={page * self.page_size}"
                    )
                items = payload.get("objects") or []
                if not items:
                    break
                new_count = 0
                for item in items:
                    job = self._parse(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1
                if new_count == 0 or len(items) < self.page_size:
                    break

            if jobs:
                sem = asyncio.Semaphore(self.detail_concurrency)
                await asyncio.gather(*(
                    self._enrich_from_detail_api(client, sem, job)
                    for job in jobs
                ))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        *,
        offset: int,
    ) -> dict[str, Any]:
        params = {
            "company_size": 0,
            "isLandingPage": "true",
            "job_type": 0,
            "limit": self.page_size,
            "offset": offset,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    API_URL,
                    params=params,
                    headers=_request_headers(),
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Instahyre fetch failed at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Instahyre returned non-JSON at offset={offset}: {exc}"
                    ) from exc
                if not isinstance(data, dict):
                    raise ScraperError(
                        f"Instahyre job_search JSON was not an object at offset={offset}"
                    )
                return data
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Instahyre returned {response.status_code} at "
                        f"offset={offset} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Instahyre returned {response.status_code} at offset={offset}"
            )
        raise ScraperError(
            f"Instahyre exhausted retries at offset={offset}: {last_exc}"
        )

    async def _enrich_from_detail_api(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        if not job.ats_id:
            return
        detail = await self._fetch_detail_json(client, sem, job.ats_id)
        if detail is None:
            return
        if str(detail.get("id")) != job.ats_id:
            return
        composed = _compose_description(detail)
        if composed:
            job.description = composed
        loc = _format_location(detail.get("locations"))
        if loc:
            job.location = loc

    async def _fetch_detail_json(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job_id: str,
    ) -> dict[str, Any] | None:
        url = f"{API_URL}/{job_id}"
        async with sem:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = await client.get(url, headers=_request_headers())
                except httpx.HTTPError:
                    if attempt == MAX_RETRIES:
                        return None
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        return None
                    return data if isinstance(data, dict) else None
                if response.status_code in (429,) or 500 <= response.status_code < 600:
                    if attempt == MAX_RETRIES:
                        return None
                    await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                    continue
                return None

    def _parse(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        title = (item.get("title") or item.get("candidate_title") or "").strip()
        url = (item.get("public_url") or "").strip()
        if raw_id is None or not title or not url:
            return None

        employer = item.get("employer") or {}
        company = (employer.get("company_name") or "").strip() or "Unknown"
        raw: dict[str, Any] = {}
        keywords = item.get("keywords") or []
        if isinstance(keywords, list) and keywords:
            raw["keywords"] = keywords
        for source, dest in (
            ("id", "employer_id"),
            ("company_tagline", "company_tagline"),
            ("employee_count", "employee_count"),
            ("company_founded", "company_founded"),
        ):
            value = employer.get(source)
            if value not in (None, ""):
                raw[dest] = value
        if item.get("accept_outstation") is not None:
            raw["accept_outstation"] = item["accept_outstation"]

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.INSTAHYRE,
            ats_id=str(raw_id),
            location=_format_location(item.get("locations")),
            description=_compose_description(item),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _compose_description(item: dict[str, Any]) -> str | None:
    """Plain-text body from Instahyre JSON fields only (no HTML)."""
    parts: list[str] = []
    title = (item.get("title") or item.get("candidate_title") or "").strip()
    if title:
        parts.append(title)
    loc = _format_location(item.get("locations"))
    if loc:
        parts.append(f"Location: {loc}")
    keywords = item.get("keywords")
    if isinstance(keywords, list) and keywords:
        flat = ", ".join(
            str(k).strip() for k in keywords if k and str(k).strip()
        )
        if flat:
            parts.append(f"Skills: {flat}")
    employer = item.get("employer") or {}
    company = (employer.get("company_name") or "").strip()
    tagline = (employer.get("company_tagline") or "").strip()
    if company and tagline:
        parts.append(f"{company} — {tagline}")
    elif company or tagline:
        parts.append(company or tagline)
    note = (employer.get("instahyre_note") or "").strip()
    if note:
        parts.append(f"About the company: {note}")
    if not parts:
        return None
    text = "\n\n".join(parts)
    return text[:10_000] if text else None


def _format_location(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return ", ".join(parts) if parts else None
