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

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_detail(client, sem, copy)
            return copy.description

        return asyncio.run(run())

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
            if self.include_descriptions and all_jobs:
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
                "`pip install jobhive-py[scrapers]` or `pip install beautifulsoup4`."
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
        job.description = description[:25_000]

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


_GOOGLE_SECTION_HEADINGS = (
    "About the job",
    "Minimum qualifications",
    "Preferred qualifications",
    "Responsibilities",
    "Benefits",
)


def _extract_full_description(html: str) -> str | None:
    """Pull the full job-detail body, section by section.

    Google's career page splits the description into separate sibling
    containers (``KwJkGe``-class divs etc.) rather than a single parent
    that wraps everything. The previous "find one container with all
    markers" heuristic could match a partial container — typically one
    holding *About the job* + *Responsibilities* but not the
    *Minimum/Preferred qualifications* sections — and we were silently
    publishing incomplete descriptions.

    We now scan for each known h3 heading independently and stitch them
    together in the order listed in ``_GOOGLE_SECTION_HEADINGS``.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return _meta_description(html)

    soup = BeautifulSoup(html, "html.parser")

    sections: list[tuple[str, str]] = []
    for heading_label in _GOOGLE_SECTION_HEADINGS:
        h = soup.find(
            "h3",
            string=lambda s, lbl=heading_label: bool(
                s and s.strip().rstrip(":").lower() == lbl.lower()
            ),
        )
        if h is None:
            continue
        # The section body lives in the h3's nearest enclosing div that
        # also contains the answer (a sibling ``ul``/``p``/``div``).
        # Climb up at most 4 ancestors to find a container that contains
        # both the heading and the body text; otherwise fall back to
        # collecting all following siblings until the next h3.
        body_text = _collect_section_body(h)
        if body_text:
            sections.append((heading_label, body_text))

    if not sections:
        return _meta_description(html)

    parts: list[str] = []
    for label, body in sections:
        if label == "About the job":
            parts.append(body)
        else:
            parts.append(f"{label}\n{body}")
    text = "\n\n".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _collect_section_body(heading_node) -> str:
    """Return the human-readable body text under a section h3.

    Climbs up to find a container that holds both the heading and its
    answer (lists/paragraphs are usually a sibling div, not a direct
    child of the h3). Falls back to walking the following siblings of
    the heading until the next h3 — handles flat layouts where
    headings + bodies are siblings rather than nested.
    """
    # Strategy 1: ancestor that includes a list or paragraph after the h3
    node = heading_node
    for _ in range(4):
        node = node.find_parent()
        if node is None:
            break
        # If this ancestor contains a ul/ol/p after the heading and is
        # otherwise focused on this one section, use it.
        lists = node.find_all(["ul", "ol", "p"], recursive=True)
        if lists:
            # Only accept ancestors that don't contain another h3 (that
            # would mean we picked up multiple sections in one ancestor).
            other_h3 = [h for h in node.find_all("h3", recursive=True) if h is not heading_node]
            if not other_h3:
                text = node.get_text(separator="\n", strip=True)
                # Strip the heading itself from the start
                heading_text = heading_node.get_text(strip=True)
                if text.startswith(heading_text):
                    text = text[len(heading_text):].lstrip(":\n ")
                return text

    # Strategy 2: walk following-siblings of the heading
    parts: list[str] = []
    for sib in heading_node.next_siblings:
        if getattr(sib, "name", None) == "h3":
            break
        if hasattr(sib, "get_text"):
            t = sib.get_text(separator="\n", strip=True)
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


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
