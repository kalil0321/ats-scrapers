"""jobs.cz — Czech Republic's leading job board (~20-50k live postings).

Companies post directly to jobs.cz (operated by Alma Career, formerly LMC).
The bulk of postings are Czech-language (``cs``); some multinational employers
post in English as well. Coverage is nationwide and spans every sector.

Server-rendered HTML listings only — there is no public JSON API. We scrape
``https://www.jobs.cz/prace/<city>/?page=N`` and parse the ``article.SearchResultCard``
nodes. Each card embeds:

  - ``data-jobad-id`` — stable per-posting id (the "rpd" id used in detail URLs)
  - ``data-test-ad-title`` — title (also rendered inside the link text)
  - ``<span translate="no">`` — employer name
  - ``li[data-test="serp-locality"]`` — location text
  - ``span.Tag.Tag--success`` — salary range in CZK, when present
  - ``span.Tag.Tag--neutral`` — soft attributes (modality, response time, …)

Two important quirks of the listing pages:

  - The ``/prace/`` root URL renders an empty results pane (the SPA fetches
    via an internal API the public scraper can't hit). We must scrape a
    locality-filtered URL like ``/prace/praha/`` to get server-rendered
    cards.

  - Pagination silently caps. Each location seed exposes ~45 pages × 30
    cards = ~1350 unique postings. ``?page=200`` returns the last available
    page rather than 404. To cover the full ~30k+ database we run a fan-out
    across the major Czech cities and dedupe by ``data-jobad-id``.

Listing pages do not surface ``employment_type`` or ``posted_at`` —
those live on the detail page only, which is heavily JS-driven and
worth skipping for the listing scrape. Salary parses from the Czech
"X 000 – Y 000 Kč" / "X 000 Kč" Tag text.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
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

BASE_URL = "https://www.jobs.cz"
LISTING_TEMPLATE = "https://www.jobs.cz/prace/{seed}/"
PER_PAGE = 30  # cards rendered per listing page (server-side constant)
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
# The listing pager silently clamps deep page numbers to the last
# available page. ~45 is the observed cap for any single locality seed
# (2026-05-12), but use a slightly higher cap so smaller seeds with
# fewer pages exit on the dedup signal rather than a hard ceiling.
DEFAULT_MAX_PAGES = 60

# Major Czech cities — each ``/prace/<seed>/`` view returns a different
# slice of the ~30k+ live postings. After dedup by ``data-jobad-id``
# the union approaches the full database. Names use the unicode-safe
# URL slug form jobs.cz expects (no diacritics, dashes for spaces).
_LOCATION_SEEDS: tuple[str, ...] = (
    "praha",
    "brno",
    "ostrava",
    "plzen",
    "liberec",
    "olomouc",
    "usti-nad-labem",
    "ceske-budejovice",
    "hradec-kralove",
    "pardubice",
    "zlin",
    "jihlava",
    "karlovy-vary",
    # ``zahranici`` (foreign) catches CZ-listed roles posted from abroad
    # and any "Czech Republic"-wide postings missed by the city slices.
    "zahranici",
)

# Czech employment-type label → canonical EmploymentType enum value.
# These labels appear on detail pages and occasionally as Tags on the
# listing card; the lookup is kept here as a single source of truth so
# both contexts use the same normalization.
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "plný úvazek": "FULL_TIME",
    "hlavní pracovní poměr": "FULL_TIME",
    "částečný úvazek": "PART_TIME",
    "brigáda": "PART_TIME",
    "stáž": "INTERN",
    "dohoda o provedení práce": "CONTRACT",
    "dohoda o provedení činnosti": "CONTRACT",
    "dohoda o pracovní činnosti": "CONTRACT",
}

# Salary tag text: "40 000 Kč", "55 000 – 60 000 Kč", "60 000 ‍–‍ 70 000 Kč".
# The U+200D zero-width-joiner is rendered by jobs.cz between the two
# bound digits. Numbers contain literal spaces as thousand separators.
_SALARY_RE = re.compile(
    r"(\d[\d\s ]*)\s*(?:[–\-]\s*(\d[\d\s ]*))?\s*Kč",
    re.UNICODE,
)


@ScraperRegistry.register(ATSType.JOBSCZ)
class JobsCzScraper(BaseScraper):
    """jobs.cz (Czech Republic) — direct-posting job board.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap per location seed (default 60).
    - ``location_seeds`` — override the location seed list (pass ``()``
      to disable seeding entirely; the empty-seed path is exposed for
      unit tests, not for production scrapes).
    """

    ats = ATSType.JOBSCZ

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        location_seeds: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max(1, max_pages)
        # ``None`` keeps the production default (full seed list); pass
        # ``()`` to disable seeding entirely for tests.
        self.location_seeds: tuple[str, ...] = (
            _LOCATION_SEEDS if location_seeds is None else tuple(location_seeds)
        )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        seeds = self.location_seeds

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            for seed in seeds:
                try:
                    slice_jobs = await self._run_seed(client, sem, seed)
                except ScraperError as exc:
                    # Per-seed failures should not blow up the whole run —
                    # the next seed still contributes its rows.
                    log.warning("jobs.cz: seed=%s failed: %s", seed, exc)
                    continue
                new_count = 0
                for job in slice_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    all_jobs.append(job)
                    new_count += 1
                log.info(
                    "jobs.cz: seed=%s → %d rows (%d new, total %d)",
                    seed, len(slice_jobs), new_count, len(all_jobs),
                )

        return all_jobs

    async def _run_seed(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        seed: str,
    ) -> list[Job]:
        """Walk one city seed sequentially until the pager loops back to
        an already-seen page or the ``max_pages`` ceiling is hit.

        Sequential rather than fanned-out: we stop as soon as we detect
        the page-clamp (the same set of ids reappearing). Fanned-out
        requests would waste round trips against the silent cap.
        """
        seen_ids: set[str] = set()
        jobs: list[Job] = []

        for page in range(1, self.max_pages + 1):
            url = LISTING_TEMPLATE.format(seed=seed)
            params = {"page": page} if page > 1 else None
            html = await self._fetch_page(client, sem, url, params)
            page_jobs = list(_parse_listing(html))
            if not page_jobs:
                break
            new_on_page = 0
            for job in page_jobs:
                if job.ats_id in seen_ids:
                    continue
                seen_ids.add(job.ats_id)
                jobs.append(job)
                new_on_page += 1
            # Pager clamp: when an entire page is duplicates of the prior
            # pages we've crossed the last real page — stop.
            if new_on_page == 0:
                break

        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        params: dict[str, int] | None,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with sem:
                    response = await client.get(
                        url,
                        params=params,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"jobs.cz fetch failed for {url} "
                        f"params={params}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # A seed that no longer exists — treat as empty.
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"jobs.cz returned {response.status_code} for "
                        f"{url} params={params} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"jobs.cz returned {response.status_code} for {url} "
                f"params={params}"
            )
        raise ScraperError(
            f"jobs.cz exhausted retries for {url} params={params}: "
            f"{last_exc}"
        )


def _parse_listing(html: str) -> list[Job]:
    """Parse an ``article.SearchResultCard`` per posting from the listing
    page HTML and return ``Job`` instances. Cards that can't be parsed
    into a minimum-viable Job (missing id, missing title) are skipped
    with a debug-level log rather than crashing the whole page.
    """
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - bs4 is a hard dep
        raise ScraperError(
            "jobs.cz scraper requires beautifulsoup4 (bs4) for HTML parsing"
        ) from exc

    soup = BeautifulSoup(html, "html.parser")
    fetched_at = datetime.now(UTC)
    jobs: list[Job] = []
    for card in soup.select("article.SearchResultCard"):
        job = _parse_card(card, fetched_at=fetched_at)
        if job is not None:
            jobs.append(job)
    return jobs


def _parse_card(card: Any, *, fetched_at: datetime) -> Job | None:
    link = card.select_one("a[data-jobad-id]")
    if link is None:
        return None
    ats_id = (link.get("data-jobad-id") or "").strip()
    if not ats_id:
        return None
    title = (link.get("data-test-ad-title") or link.get_text(strip=True) or "").strip()
    if not title:
        return None
    href = link.get("href") or ""
    if not href:
        return None
    url = _absolutize(href)
    # Strip session-tracking ``?searchId=...&rps=...`` query so the
    # canonical URL is stable across scrapes. Jobs.cz mints a fresh
    # ``searchId`` on every listing render — keeping it would flap
    # diffs on every run.
    url = url.split("?", 1)[0]

    company_el = card.select_one("span[translate='no']")
    company = company_el.get_text(strip=True) if company_el else ""

    loc_el = card.select_one("li[data-test='serp-locality']")
    location = loc_el.get_text(" ", strip=True) if loc_el else None

    # Soft attributes — "Možnost občasné práce z domova" (remote-friendly),
    # "Odpověď do 2 týdnů" (response time SLA), modality tags. We keep
    # them as a single ``modality`` string in ``raw`` so the LLM
    # enrichment pass can use them without re-fetching the listing.
    body = card.select_one("div.SearchResultCard__body")
    tag_texts: list[str] = []
    salary_text: str | None = None
    if body is not None:
        for tag in body.select("span.Tag"):
            text = tag.get_text(" ", strip=True)
            if not text:
                continue
            # Tag--success carries the salary string; everything else
            # (Tag--neutral) is a modality / response-time attribute.
            css_class = " ".join(tag.get("class") or [])
            if "Tag--success" in css_class:
                salary_text = _normalize_whitespace(text)
            else:
                tag_texts.append(text)

    salary_min, salary_max, salary_summary = _parse_salary(salary_text)

    employment_type = _employment_type_from_tags(tag_texts)

    raw: dict[str, Any] = {}
    if tag_texts:
        raw["modality"] = tag_texts

    return Job(
        url=url,
        title=title,
        company=company or "Unknown",
        ats_type=ATSType.JOBSCZ,
        ats_id=ats_id,
        location=location,
        country_iso="CZ",
        language="cs",
        salary_currency="CZK" if salary_min is not None or salary_summary else None,
        salary_period="MONTH" if salary_min is not None or salary_summary else None,
        salary_summary=salary_summary,
        salary_min=salary_min,
        salary_max=salary_max,
        employment_type=employment_type,
        fetched_at=fetched_at,
        raw=raw or None,
    )


def _absolutize(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return f"{BASE_URL}{href}"
    return f"{BASE_URL}/{href}"


def _normalize_whitespace(text: str) -> str:
    """Collapse non-breaking spaces and zero-width joiners that jobs.cz
    interleaves into the salary text. Preserves the ``–`` range
    separator so callers can still pattern-match it."""
    out = text.replace("‍", "").replace(" ", " ")
    return re.sub(r"\s+", " ", out).strip()


def _parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse "55 000 – 60 000 Kč" / "60 000 Kč" into (min, max, summary).

    Returns ``(None, None, None)`` when no salary text is present.
    Falls back to ``(None, None, summary)`` if the text is present but
    doesn't match the expected shape — keeps the user-facing string for
    downstream enrichment.
    """
    if not text:
        return None, None, None
    summary = text
    match = _SALARY_RE.search(text)
    if not match:
        return None, None, summary
    lo_raw = match.group(1)
    hi_raw = match.group(2)
    lo = _parse_amount(lo_raw)
    hi = _parse_amount(hi_raw) if hi_raw else None
    return lo, hi, summary


def _parse_amount(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[\s ]+", "", raw)
    if not cleaned.isdigit():
        return None
    try:
        return float(cleaned)
    except ValueError:  # pragma: no cover - guarded by isdigit
        return None


def _employment_type_from_tags(tags: list[str]) -> str | None:
    """Map any of the Czech employment-type labels found among the
    card's neutral tags to the canonical ``EmploymentType`` enum.
    Most cards don't expose this on the listing page — they get
    ``None`` and the downstream enrichment fills the gap from the
    detail page when needed.
    """
    for tag in tags:
        lower = tag.lower()
        for needle, value in _EMPLOYMENT_TYPE_MAP.items():
            if needle in lower:
                return value
    return None
