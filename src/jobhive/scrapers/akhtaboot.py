"""Akhtaboot — Middle East direct employer job board."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://www.akhtaboot.com"
LISTING_URL = f"{BASE_URL}/en/the-middle-east/jobs"
DEFAULT_MAX_PAGES = 20
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_JOB_BLOCK_RE = re.compile(
    r"<div class='job clearfix'>(?P<body>.*?)"
    r"(?=<div class='job clearfix'>|<div class='col-xs-12 text-center|$)",
    re.DOTALL,
)
_REF_RE = re.compile(r"Ref\.\s*Number:\s*(?P<id>\d+)", re.IGNORECASE)
_DATE_RE = re.compile(r"Date Posted:\s*(?P<date>\d{2}-\d{2}-\d{4})", re.IGNORECASE)
_LINK_RE = re.compile(
    r"<a class='job-link' href='(?P<href>[^']+)'[^>]*>\s*<h4>(?P<title>.*?)</h4>",
    re.DOTALL,
)
_COMPANY_LOC_RE = re.compile(
    r"<p class='no-margin'>\s*<strong>\s*(?P<company>.*?)\s*-\s*"
    r"<span>\s*(?P<location>.*?)\s*</span>",
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.AKHTABOOT)
class AkhtabootScraper(BaseScraper):
    """Akhtaboot MENA jobs scraper."""

    ats = ATSType.AKHTABOOT

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
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page in range(1, self.max_pages + 1):
                text = await self._fetch_page(client, page=page)
                page_jobs = self._parse_page(text)
                if not page_jobs:
                    break
                new_count = 0
                for job in page_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1
                if new_count == 0:
                    break
        return jobs

    async def _fetch_page(self, client: httpx.AsyncClient, *, page: int) -> str:
        params: dict[str, Any] = {"page": page, "per_page": 25}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    LISTING_URL,
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "text/html,*/*",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Akhtaboot fetch failed at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Akhtaboot returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Akhtaboot returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"Akhtaboot exhausted retries at page={page}: {last_exc}")

    def _parse_page(self, text: str) -> list[Job]:
        jobs: list[Job] = []
        for match in _JOB_BLOCK_RE.finditer(text):
            job = self._parse_block(match.group("body"))
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_block(self, block: str) -> Job | None:
        ref = _REF_RE.search(block)
        link = _LINK_RE.search(block)
        company_loc = _COMPANY_LOC_RE.search(block)
        if not ref or not link:
            return None

        ats_id = ref.group("id")
        title = _clean_html(link.group("title"))
        href = html.unescape(link.group("href"))
        if not title:
            return None

        company = "Unknown"
        location = None
        if company_loc:
            company = _clean_html(company_loc.group("company")) or "Unknown"
            location = _clean_html(company_loc.group("location")) or None

        posted_at = None
        date_match = _DATE_RE.search(block)
        if date_match:
            posted_at = _parse_date(date_match.group("date"))

        return Job(
            url=href if href.startswith("http") else f"{BASE_URL}{href}",
            title=title,
            company=company,
            ats_type=ATSType.AKHTABOOT,
            ats_id=ats_id,
            location=location,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw={"source_region": "Middle East"},
        )


def _clean_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return " ".join(text.split())


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d-%m-%Y")
    except ValueError:
        return None
