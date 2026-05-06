"""Google careers scraper.

Google's careers site at:

    GET https://www.google.com/about/careers/applications/jobs/results
        ?hl=en_US&page=N

Has no public JSON API. The HTML uses obfuscated CSS class names that
change periodically, so the legacy ``data/google/main.py`` instead targets
the stable ``aria-label="Learn more about ..."`` attribute Google attaches
to each job link for accessibility — that's what we use here too.

Pagination: incrementing ``page`` until a page yields no new job IDs (the
markup doesn't expose a total count).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

LISTING_URL = "https://www.google.com/about/careers/applications/jobs/results"
APPLICATIONS_BASE = "https://www.google.com/about/careers/applications/"

MAX_PAGES = 500  # Defensive ceiling. Google currently exposes ~180 pages (~3,600 jobs) and we stop on a no-new-ids page; 100 was hard-capping us at exactly 2,000.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


@ScraperRegistry.register(ATSType.GOOGLE)
class GoogleScraper(BaseScraper):
    """Google scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.GOOGLE

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page_num in range(1, MAX_PAGES + 1):
                html_text = await self._fetch_page(client, page_num)
                page_jobs = self._parse_page(html_text)
                new = [j for j in page_jobs if j.ats_id not in seen]
                if not new:
                    # Page yielded zero new IDs — we've seen everything.
                    break
                for j in new:
                    seen.add(j.ats_id)
                all_jobs.extend(new)
        return all_jobs

    async def _fetch_page(self, client: httpx.AsyncClient, page: int) -> str:
        params: dict[str, str | int] = {"hl": "en_US"}
        if page > 1:
            params["page"] = page
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    LISTING_URL, params=params, headers=_HEADERS
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Google fetch failed at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Google returned {response.status_code} at page={page} "
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
                f"Google returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"Google exhausted retries at page={page}")

    def _parse_page(self, html_text: str) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ScraperError(
                "Google scraper requires beautifulsoup4. Install with "
                "`pip install jobhive[scrapers]` or `pip install beautifulsoup4`."
            ) from exc

        soup = BeautifulSoup(html_text, "html.parser")
        jobs: list[Job] = []
        seen: set[str] = set()

        # Stable selector: every job link carries `aria-label="Learn more about <Title>"`.
        # The visible CSS classes on the page rotate but the aria-label is part of
        # Google's accessibility contract.
        for anchor in soup.find_all("a", attrs={"aria-label": True, "href": True}):
            aria = anchor["aria-label"]
            if not aria.startswith("Learn more about"):
                continue
            href = anchor["href"]
            full_url = _canonicalize(urljoin(APPLICATIONS_BASE, href))
            ats_id = _extract_id(full_url)
            if not ats_id or ats_id in seen:
                continue
            seen.add(ats_id)
            title = aria.removeprefix("Learn more about").strip() or "Untitled"
            jobs.append(
                Job(
                    url=full_url,
                    title=title,
                    company="Google",
                    ats_type=ATSType.GOOGLE,
                    ats_id=ats_id,
                    location=None,
                    posted_at=None,
                    fetched_at=datetime.now(),
                )
            )
        return jobs


def _canonicalize(url: str) -> str:
    """Strip query params (`?hl=en_US&_gl=...`) and fragments — multiple anchors
    on the same page sometimes link to the same job with different query
    suffixes; canonicalizing collapses them."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _extract_id(url: str) -> str:
    """Job URL form: `/jobs/results/{numeric_id}-{slug-title}`. Take the
    numeric prefix as the canonical ID."""
    path = urlsplit(url).path
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last.split("-", 1)[0] if "-" in last else last
