"""MyJobMag (https://www.myjobmag.com) — pan-African jobs scraper.

MyJobMag is a multi-country direct-posting job board covering Nigeria,
Ghana, Kenya, Uganda, and South Africa, with a shared HTML engine
across regional subdomains. Each region runs the same listing
template (``<li class="job-list-li">`` cards on ``/jobs/page/N``) so
one parser handles all five.

Country selection is via ``company_slug``:

- ``"ng"`` → ``https://www.myjobmag.com`` (Nigeria, default)
- ``"gh"`` → ``https://www.myjobmagghana.com``
- ``"ke"`` → ``https://www.myjobmag.co.ke``
- ``"ug"`` → ``https://www.myjobmag.co.ug``
- ``"za"`` → ``https://www.myjobmag.co.za``

Aliases (``""``, ``"any"``, ``"nigeria"``, full country names) resolve
to the Nigeria default, so the scraper is registry-friendly even when
the slug isn't a region code.

The listing page surfaces a handful of structured fields per card
(title, company, posted date, ~200-char summary, slug-based ats_id).
Job-detail pages on the ``.com`` / ``.co.ke`` / ``.co.za`` properties
also embed a JSON-LD ``JobPosting`` block with employmentType,
addressCountry, occupationalCategory, and the full description. The
scraper deliberately stays at the listing level for the default sweep
so one full pass is N requests instead of N×~30 — downstream
enrichment can fetch detail pages where richer data is required.

Pagination follows ``/jobs/page/N``. The site never returns an empty
results page (page 9999 still renders 20+ cards), so we dedup on
``ats_id`` and stop when a fetched page introduces zero new IDs.
"""

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


# slug → (base_url, ISO 3166-1 alpha-2, ISO 639-1 language, ISO 4217 currency)
REGIONS: dict[str, tuple[str, str, str, str]] = {
    "ng": ("https://www.myjobmag.com", "NG", "en", "NGN"),
    "gh": ("https://www.myjobmagghana.com", "GH", "en", "GHS"),
    "ke": ("https://www.myjobmag.co.ke", "KE", "en", "KES"),
    "ug": ("https://www.myjobmag.co.ug", "UG", "en", "UGX"),
    "za": ("https://www.myjobmag.co.za", "ZA", "en", "ZAR"),
}

# Aliases mapped to the canonical 2-letter region code. The
# ScraperRegistry instantiates with the company-CSV slug — being
# tolerant of free-form values keeps live runs from crashing when
# a discovery pass writes ``nigeria`` instead of ``ng``.
_REGION_ALIASES: dict[str, str] = {
    "": "ng", "any": "ng",
    "nigeria": "ng", "ng": "ng",
    "ghana": "gh", "gh": "gh",
    "kenya": "ke", "ke": "ke",
    "uganda": "ug", "ug": "ug",
    "south africa": "za", "south-africa": "za",
    "southafrica": "za", "za": "za",
}

MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
# Cap pagination so misbehaving sites can't run away — the live boards
# carry ~10k active rows at most (≈250 pages of 40), so 400 is a comfy
# ceiling that's still well below an accidental infinite loop.
DEFAULT_MAX_PAGES = 400

# ``<li class="job-list-li">`` is the per-card wrapper. The list also
# contains AdSense placeholder ``<li class="job-list-li">`` rows with
# no ``job-info`` child — those are filtered out downstream.
_CARD_START_RE = re.compile(r'<li class="job-list-li"', re.IGNORECASE)

# Inside a card: the main posting title sits in
# ``<li class="mag-b"><h2><a href="/job/...">Title at Company</a></h2></li>``.
# Rollup entries link to ``/jobs/...`` (e.g. "Latest Jobs at X") and are
# skipped — those expand into sub-job entries we capture separately.
_MAIN_LINK_RE = re.compile(
    r'<h2>\s*<a[^>]*\bhref="(/job/[^"]+)"[^>]*>(.*?)</a>\s*</h2>',
    re.IGNORECASE | re.DOTALL,
)
_JOBS_AT_RE = re.compile(
    r'<a[^>]*\bhref="/jobs-at/[^"]+"[^>]*>\s*<img[^>]*\balt="([^"]*)"',
    re.IGNORECASE,
)
_DESC_RE = re.compile(
    r'<li class="job-desc">\s*(.*?)\s*</li>',
    re.IGNORECASE | re.DOTALL,
)
_DATE_RE = re.compile(
    r'id="job-date">\s*([^<]+?)\s*</li>',
    re.IGNORECASE,
)
# Sub-job-section contains a flat list of related single postings under a
# rollup or company card. Each ``<a href="/job/slug">Title</a>``.
_SUB_JOB_RE = re.compile(
    r'<a[^>]*\bhref="(/job/[^"]+)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Per-detail-page JSON-LD JobPosting block. Used only by the optional
# enrichment helper; left as a class-level constant so tests can target it.
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)

# JSON-LD employmentType → canonical ``EmploymentType`` enum value. The
# site mixes labels (sometimes ``"Full Time , Onsite"`` — comma-joined
# tags) so we tokenize and match the first known value.
_EMPLOYMENT_MAP: dict[str, str] = {
    "full time": "FULL_TIME",
    "full_time": "FULL_TIME",
    "part time": "PART_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
}

# "11 May", "31 Dec", etc. — the listing page uses day-month with no
# year. Without a year we'd risk binding April-2024 rows to today's year
# at year-end (a 1-day-old post appears as 364 days old), so we treat
# the listing date as best-effort and bias toward "this year if not in
# the future, else previous year".
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


@ScraperRegistry.register(ATSType.MYJOBMAG)
class MyJobMagScraper(BaseScraper):
    """MyJobMag — pan-African direct-posting jobs board.

    ``company_slug`` selects the regional property:
    ``"ng"`` / ``"gh"`` / ``"ke"`` / ``"ug"`` / ``"za"`` (plus
    full-country-name aliases). Empty / ``"any"`` defaults to Nigeria,
    which is the largest property and the canonical entry point.
    """

    ats = ATSType.MYJOBMAG

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        region_key = _REGION_ALIASES.get(company_slug.strip().lower())
        if region_key is None:
            raise ScraperError(
                f"Unknown MyJobMag region {company_slug!r}. Supported: "
                f"{sorted(REGIONS)}"
            )
        base_url, country_iso, language, currency = REGIONS[region_key]
        self._region = region_key
        self._base_url = base_url
        self._country_iso = country_iso
        self._language = language
        self._currency = currency

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            # Pagination is best-handled serially: the site doesn't ship
            # a page-count, so we stop the first time a page contributes
            # zero new ats_ids. Fanning out optimistically would mean
            # over-fetching once we've passed the natural end.
            page = 1
            while page <= self.max_pages:
                html_text = await self._fetch_page(client, sem, page=page)
                rows = self._parse_listing(html_text)
                new_in_page = 0
                for row in rows:
                    if row.ats_id in seen:
                        continue
                    seen.add(row.ats_id)
                    jobs.append(row)
                    new_in_page += 1
                if new_in_page == 0:
                    break
                page += 1
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> str:
        url = f"{self._base_url}/jobs/page/{page}"
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"MyJobMag fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # Past-end-of-pagination signal on some properties.
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"MyJobMag returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"MyJobMag returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"MyJobMag exhausted retries for {url}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_listing(self, html_text: str) -> list[Job]:
        """Split a listing page into cards and emit one ``Job`` per
        single-posting card (plus per sub-job inside rollup cards)."""
        if not html_text:
            return []
        # Carve the document into ``<li class="job-list-li">`` chunks
        # via start-marker positions; the trailing close-tag is shared
        # with the parent ``<ul>``, so the cheapest reliable split is
        # "from one start to the next".
        starts = [m.start() for m in _CARD_START_RE.finditer(html_text)]
        if not starts:
            return []
        jobs: list[Job] = []
        seen_in_page: set[str] = set()
        fetched_at = datetime.now()
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(html_text)
            chunk = html_text[start:end]
            # AdSense placeholder cards have no job-info ul.
            if "job-info" not in chunk and "sub-job-sec" not in chunk:
                continue
            company_alt = _JOBS_AT_RE.search(chunk)
            company_from_logo = (
                _clean(company_alt.group(1)) if company_alt else None
            )
            desc_match = _DESC_RE.search(chunk)
            desc_text = (
                _strip_html(desc_match.group(1)) if desc_match else None
            )
            date_match = _DATE_RE.search(chunk)
            posted_at = (
                _parse_listing_date(date_match.group(1), fetched_at)
                if date_match else None
            )

            # Main posting (single-job card). The href looks like
            # ``/job/<slug>`` for single jobs and ``/jobs/...`` for
            # rollups — _MAIN_LINK_RE only matches the former.
            main = _MAIN_LINK_RE.search(chunk)
            if main:
                href, raw_title = main.group(1), main.group(2)
                job = self._build_job(
                    href=href,
                    raw_title=raw_title,
                    company_fallback=company_from_logo,
                    description=desc_text,
                    posted_at=posted_at,
                    fetched_at=fetched_at,
                )
                if job and job.ats_id not in seen_in_page:
                    seen_in_page.add(job.ats_id or "")
                    jobs.append(job)

            # Sub-jobs inside rollup cards: take the
            # ``<li class="sub-job-sec">`` block and emit one per
            # ``<a href="/job/...">`` link. Skip these if there's no
            # sub-job section (i.e. pure single-job cards).
            sub_section = re.search(
                r'<li class="sub-job-sec">(.*?)</li>\s*</ul>\s*</li>',
                chunk, re.DOTALL | re.IGNORECASE,
            )
            if sub_section:
                for sub_match in _SUB_JOB_RE.finditer(sub_section.group(1)):
                    sub_href, sub_title = sub_match.group(1), sub_match.group(2)
                    sub_job = self._build_job(
                        href=sub_href,
                        raw_title=sub_title,
                        company_fallback=company_from_logo,
                        description=None,
                        # Sub-job rows share the rollup's date row.
                        posted_at=posted_at,
                        fetched_at=fetched_at,
                    )
                    if sub_job and sub_job.ats_id not in seen_in_page:
                        seen_in_page.add(sub_job.ats_id or "")
                        jobs.append(sub_job)

        return jobs

    def _build_job(
        self,
        *,
        href: str,
        raw_title: str,
        company_fallback: str | None,
        description: str | None,
        posted_at: datetime | None,
        fetched_at: datetime,
    ) -> Job | None:
        slug = href.rsplit("/", 1)[-1].strip()
        if not slug:
            return None
        cleaned_title = _strip_html(raw_title)
        if not cleaned_title:
            return None
        title, company = _split_title_company(cleaned_title)
        if not company and company_fallback:
            company = company_fallback
        if not company:
            # Best-effort: derive from slug tail (e.g. ``...-acme-corp``).
            company = "Unknown"
        url = (
            href if href.startswith("http")
            else f"{self._base_url}{href}"
        )
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.MYJOBMAG,
            ats_id=slug,
            country_iso=self._country_iso,
            language=self._language,
            description=description,
            posted_at=posted_at,
            fetched_at=fetched_at,
        )


# --- helpers ----------------------------------------------------------------


def _clean(value: str) -> str:
    return _WS_RE.sub(" ", html.unescape(value)).strip()


def _strip_html(value: str) -> str:
    text = _TAG_RE.sub(" ", html.unescape(value))
    text = _WS_RE.sub(" ", text).strip()
    return text[:10_000]


def _split_title_company(text: str) -> tuple[str, str | None]:
    """Listing titles read ``Title at Company`` — split on the LAST
    ``" at "`` so titles with their own ``at`` (``Senior Engineer
    looking at AI``) survive. Falls back to the whole string as title
    when no ``" at "`` separator is present."""
    # Prefer rightmost split: handles "Senior Specialist at Acme" but
    # leaves "Sales Associate at Heart at Acme" → title='Sales Associate
    # at Heart', company='Acme'.
    sep = " at "
    idx = text.rfind(sep)
    if idx <= 0:
        return text, None
    return text[:idx].strip(), text[idx + len(sep):].strip() or None


def _parse_listing_date(value: str, ref: datetime) -> datetime | None:
    """Parse listing dates like ``11 May`` or ``31 Dec``. The site
    omits the year, so we attribute the post to the most-recent
    occurrence that isn't in the future: if today is 12 May 2026 and
    the post says ``31 Dec``, bind it to 2025."""
    parts = value.strip().split()
    if len(parts) < 2:
        return None
    day_s, month_s = parts[0], parts[1].lower().strip(",")
    try:
        day = int(day_s)
    except ValueError:
        return None
    month = _MONTHS.get(month_s[:3])
    if month is None:
        return None
    try:
        candidate = datetime(ref.year, month, day)
    except ValueError:
        return None
    # If the candidate is in the future (more than 1 day ahead — covers
    # timezone wobble around midnight), back up to previous year.
    if (candidate - ref).total_seconds() > 86400:
        try:
            candidate = datetime(ref.year - 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_jsonld_job(html_text: str) -> dict[str, Any] | None:
    """Pull the first JSON-LD JobPosting object out of a MyJobMag
    detail page. Exposed for downstream enrichment that wants the
    richer schema (employmentType, postal address, occupational
    category) the listing doesn't carry — the default fetch path
    intentionally skips per-detail requests for throughput.

    Returns the decoded dict or ``None`` if no JobPosting is present
    (notably on ``myjobmagghana.com``, which serves a non-JSON-LD
    template — callers must fall back to the listing-only data there).

    Live MyJobMag blocks ship raw newlines inside the ``description``
    string (technically invalid JSON), so we retry parsing with
    ``strict=False`` after the first ValueError, which tolerates
    control characters in string values.
    """
    import json

    for raw in _JSONLD_RE.findall(html_text):
        obj: object | None = None
        try:
            obj = json.loads(raw)
        except ValueError:
            try:
                obj = json.loads(raw, strict=False)
            except ValueError:
                continue
        if isinstance(obj, dict) and obj.get("@type") in ("JobPosting", "Job"):
            return obj
    return None


def normalize_employment_type(value: object) -> str | None:
    """Map a JSON-LD ``employmentType`` value (string or list, sometimes
    comma-joined) to a canonical ``EmploymentType`` enum value. The
    function is exported so downstream enrichers don't reimplement the
    same comma-tokenization logic."""
    if value is None:
        return None
    if isinstance(value, list):
        candidates: list[str] = []
        for v in value:
            if isinstance(v, str):
                candidates.extend(_split_employment_tokens(v))
    elif isinstance(value, str):
        candidates = _split_employment_tokens(value)
    else:
        return None
    for token in candidates:
        # Normalize hyphens / underscores → spaces so ``Full-Time``,
        # ``Full_Time``, and ``Full Time`` all collide on the same key.
        key = re.sub(r"[-_]+", " ", token.strip().lower())
        key = _WS_RE.sub(" ", key).strip()
        mapped = _EMPLOYMENT_MAP.get(key)
        if mapped:
            return mapped
    return None


def _split_employment_tokens(value: str) -> list[str]:
    # MyJobMag joins flags as e.g. ``"Full Time , Onsite"`` — split on
    # comma AND slash so we catch "Full-Time/Contract" too.
    return [t.strip() for t in re.split(r"[,/]", value) if t.strip()]
