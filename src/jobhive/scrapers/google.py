"""Google careers scraper.

Google's careers site at:

    GET https://www.google.com/about/careers/applications/jobs/results
        ?hl=en_US&page=N

Has no public JSON API. The HTML uses obfuscated CSS class names that
change periodically, so we target two stable surfaces:

* Listing: every job link carries
  ``aria-label="Learn more about <Title>"`` (an accessibility contract).
* Detail: each job page has standard ``<meta name="description">`` and
  ``<meta property="og:title">`` tags plus Material icon "chips"
  (``<i>place</i><span>Taipei, Taiwan</span>``) for location, team,
  etc. The icon names (``place``, ``corporate_fare``) are stable.

Pagination: increment ``page`` until a page yields no new job IDs (the
markup doesn't expose a total count). After the listing pass we fan
out per-job detail fetches concurrently to fill description, location,
and team.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
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
DETAIL_CONCURRENCY = 8  # cap per-tenant concurrent detail fetches

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Chip pattern: ``<i ...>{icon_name}</i><span ...>{value}</span``. The
# inner ``<i>`` is sometimes nested when the icon is rendered with a
# tooltip; ``[^<]*`` skips over the second ``<i>`` cleanly.
_CHIP_RE = re.compile(
    r"<i[^>]+>(?P<icon>place|corporate_fare)</i>"
    r"(?:<i[^>]*>[^<]*</i>)?"
    r"<span[^>]*>(?P<value>[^<]{1,200})</span>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


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

            # Per-job detail enrichment: pull description, location, team
            # from each job's HTML detail page. Best-effort.
            if all_jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(client, sem, j) for j in all_jobs
                ))
        return all_jobs

    async def _enrich_detail(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await client.get(str(job.url), headers=_HEADERS)
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        _apply_detail_to_job(job, response.text)

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


def _apply_detail_to_job(job: Job, html: str) -> None:
    """Mutate ``job`` in place with values pulled from a Google detail page.

    Three signals:

    * ``<meta name="description">`` — the SPA-rendered "About the job"
      body, ~1-2kB. Reliable across all jobs.
    * Material-icon chips — ``place`` → location, ``corporate_fare`` →
      team/division (e.g. "YouTube", "Google Cloud"). Stable selectors
      regardless of CSS-class rotation.
    * ``<h3>About the job</h3>``-rooted container — fuller body when
      present (includes Minimum/Preferred qualifications + Responsibilities).
      Falls back to the meta description when the container can't be
      isolated.
    """
    # Description — prefer the wider h3 container; fall back to meta.
    description = _extract_full_description(html)
    if description and not job.description:
        job.description = description[:10_000]

    # Location + team chips.
    for chip in _CHIP_RE.finditer(html):
        icon = chip.group("icon")
        value = html_mod.unescape(chip.group("value")).strip()
        if not value:
            continue
        if icon == "place" and not job.location:
            job.location = value
        elif icon == "corporate_fare" and not job.team:
            job.team = value


def _extract_full_description(html: str) -> str | None:
    """Pull the full job-detail body when the page exposes it.

    Tries the ``DkhPwc`` container that wraps the focused job's title +
    icon chips + description sections. Falls back to the page's meta
    description on parse failure.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return _meta_description(html)

    soup = BeautifulSoup(html, "html.parser")

    about = soup.find("h3", string=lambda s: s and s.strip() == "About the job")
    container = None
    if about is not None:
        node = about
        for _ in range(8):
            node = node.find_parent()
            if node is None:
                break
            text = node.get_text()
            if (
                "About the job" in text
                and "Minimum qualifications" in text
                and "Responsibilities" in text
            ):
                container = node
                break

    if container is None:
        return _meta_description(html)

    # Drop the chips/share-button boilerplate that precedes
    # "About the job" — slice from the first occurrence of that header.
    text = container.get_text(separator="\n", strip=True)
    idx = text.find("About the job")
    if idx > 0:
        text = text[idx:]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _meta_description(html: str) -> str | None:
    """Pull the canonical job summary out of ``<meta name="description">``."""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
        html,
    )
    if not match:
        return None
    return html_mod.unescape(match.group(1)).strip() or None


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
