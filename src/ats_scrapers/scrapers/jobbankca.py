"""Job Bank Canada (jobbank.gc.ca) scraper.

Canada's federal-government job board, run by Employment and Social
Development Canada. ~62k live postings across private and public
sector employers. Free, unauthenticated, server-rendered HTML — there's
no public JSON API surfaced by the site.

The search endpoint paginates 25 results per page:

    GET https://www.jobbank.gc.ca/jobsearch/jobsearch?searchstring=&sort=M&page=N

Each result is a ``<article id="article-{id}">`` block containing the
title (``.noctitle``), employer (``.business``), location
(``.location``), posting date (``.date``), and salary (``.salary``) —
plus a handful of optional flag tags (``.telework``, ``.appmethod``,
``.postedonJB``, ``.new``). Salary is free text (``$25.00 hourly``,
``$110,000.00 to $150,000.00 annually``) and currency is implicitly CAD.

The full description lives on a per-job detail page; pulling it would
mean 62k extra requests for marginal gain, so we leave ``description``
empty and ship the teaser metadata only.

Single-source scraper: ``company_slug`` is informational and ignored —
every fetch returns the entire active dataset.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.jobbank.gc.ca/jobsearch/jobsearch"
POSTING_URL_TEMPLATE = "https://www.jobbank.gc.ca/jobsearch/jobposting/{id}"
PAGE_SIZE = 25  # Server renders 25 results per page; not configurable.
MAX_PAGES = 5000
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
DEFAULT_USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 60.0

# --- Parsing primitives -----------------------------------------------------
#
# Job Bank's listing HTML is server-rendered JSF — verbose but stable.
# Anchor on the unambiguous ``<article id="article-{N}">`` opener, then
# pull each ``<li class="...">`` payload with a targeted regex. We use
# ``html.unescape`` on every captured fragment so entities like ``&amp;``
# round-trip cleanly into the canonical Job schema.

_ARTICLE_OPEN_RE = re.compile(r'<article\s+id="article-(\d+)"', re.IGNORECASE)
_TITLE_RE = re.compile(
    r'<span\s+class="noctitle"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL
)
_BUSINESS_RE = re.compile(
    r'<li\s+class="business"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)
_LOCATION_RE = re.compile(
    r'<li\s+class="location"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)
_DATE_RE = re.compile(
    r'<li\s+class="date"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)
_SALARY_RE = re.compile(
    r'<li\s+class="salary"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)
_TELEWORK_RE = re.compile(
    r'<span\s+class="telework"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL
)
_APPMETHOD_RE = re.compile(
    r'<span\s+class="appmethod"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL
)
_POSTED_ON_JB_RE = re.compile(r'class="postedonJB"', re.IGNORECASE)
_NEW_FLAG_RE = re.compile(r'<span\s+class="new"', re.IGNORECASE)
_JOB_NUMBER_RE = re.compile(
    r'<li\s+class="source"[^>]*>.*?Job\s*number:.*?(\d{3,})',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Canadian province / territory abbreviations seen in location strings
# like ``Calgary, AB`` or ``Burlington (ON)``. Used to set ``country_iso``
# defensively even when the location string lacks ", Canada".
_CA_PROVINCES = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
    "ON", "PE", "QC", "SK", "YT",
})

# English month names; the listing renders dates as ``May 08, 2026``.
# French dates are rejected by ``_parse_date`` (returns None) since the
# ``?lang=fra`` variant uses ``08 mai 2026`` — the language code is
# captured separately so callers can localize if needed.
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    # Abbreviations that occasionally appear.
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3,
    "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


@ScraperRegistry.register(ATSType.JOBBANKCA)
class JobBankCAScraper(BaseScraper):
    """Job Bank Canada scraper. Single-source: ``company_slug`` is unused."""

    ats = ATSType.JOBBANKCA

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int | None = None,
        language: str = "en",
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if max_pages is not None and max_pages < 1:
            raise ScraperError(
                f"Job Bank max_pages must be positive, got {max_pages}"
            )
        self._full_catalogue = max_pages is None
        self.max_pages = min(max_pages or MAX_PAGES, MAX_PAGES)
        # ``en`` (default) → english site. ``fr`` → ``?lang=fra`` variant.
        self.language = language

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        repeated_pages = 0
        exhausted = False
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
            proxy=self.proxy,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-CA,en;q=0.9",
            },
        ) as client:
            # Sequential paging: the cheapest termination signal is "this
            # page returned 0 articles", which we can only observe in
            # order. ~2,500 pages at PAGE_SIZE=25 — the per-page cost
            # dominates, but parallel pre-fetching past the unknown last
            # page would just waste work.
            page = 1
            while page <= self.max_pages:
                html_text = await self._fetch_page(client, sem, page)
                fetched = datetime.now(tz=UTC)
                page_jobs = self._parse_page(html_text, fetched_at=fetched)
                if not page_jobs:
                    if 'id="results-count"' not in html_text:
                        raise ScraperError(
                            f"Job Bank page={page} lacked result markup"
                        )
                    if _ARTICLE_OPEN_RE.search(html_text):
                        raise ScraperError(
                            f"Job Bank page={page} contained unparseable articles"
                        )
                    exhausted = True
                    break
                added = 0
                for job in page_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    added += 1
                logger.debug(
                    "jobbankca page=%d parsed=%d new=%d cumulative=%d",
                    page, len(page_jobs), added, len(jobs),
                )
                repeated_pages = repeated_pages + 1 if added == 0 else 0
                if repeated_pages >= 2:
                    raise ScraperError(
                        "Job Bank repeated only known jobs on consecutive pages"
                    )
                page += 1
        if self._full_catalogue and not exhausted:
            raise ScraperError(
                f"Job Bank reached its {MAX_PAGES}-page safety limit before "
                "detecting the end of results"
            )
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> str:
        params: dict[str, Any] = {
            "searchstring": "",
            "sort": "M",  # ``M`` = most recent.
            "page": page,
        }
        if self.language == "fr":
            params["lang"] = "fra"
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    r = await client.get(SEARCH_URL, params=params)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if r.status_code == 200:
                return r.text
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Job Bank Canada returned {r.status_code} on page={page} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Job Bank Canada returned {r.status_code} on page={page}"
            )
        raise ScraperError(
            f"Job Bank Canada exhausted retries on page={page}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_page(
        self, html_text: str, *, fetched_at: datetime | None = None,
    ) -> list[Job]:
        """Split the page into ``<article>`` blocks and parse each.

        Blocks that fail to parse are skipped with a debug log entry —
        we never want a single malformed listing to bring down the run.
        """
        jobs: list[Job] = []
        positions = [m.start() for m in _ARTICLE_OPEN_RE.finditer(html_text)]
        if not positions:
            return jobs
        positions.append(len(html_text))
        for i in range(len(positions) - 1):
            chunk = html_text[positions[i]:positions[i + 1]]
            job = self._parse_job(chunk, fetched_at=fetched_at)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_job(
        self, chunk: str, *, fetched_at: datetime | None = None,
    ) -> Job | None:
        m = _ARTICLE_OPEN_RE.search(chunk)
        if not m:
            return None
        ats_id = m.group(1).strip()
        title = _capture_text(_TITLE_RE, chunk)
        if not ats_id or not title:
            return None

        company = _capture_text(_BUSINESS_RE, chunk) or "Job Bank Canada"
        location = _capture_text(_LOCATION_RE, chunk)
        if location:
            # Strip the leading "Location" wb-inv label that survives
            # tag-stripping ("LocationCalgary, AB" → "Calgary, AB").
            location = re.sub(r"^Location\s*", "", location).strip() or None

        # Date renders as ``May 08, 2026`` on the English site. The
        # ``.date`` <li> sometimes carries an ``"Expires May 22, 2026"``
        # second line; we strip everything past the first comma+year.
        date_raw = _capture_text(_DATE_RE, chunk)
        posted_at = _parse_date(date_raw)

        # Salary: ``Salary $42.00 to $50.00 hourly (to be negotiated)``.
        # Drop the leading "Salary" wb-inv label before storing.
        salary_raw = _capture_text(_SALARY_RE, chunk)
        salary_summary = None
        salary_currency = None
        salary_period = None
        if salary_raw:
            salary_summary = re.sub(r"^Salary\s*", "", salary_raw).strip() or None
            salary_period = _salary_period(salary_summary)
            if salary_summary and "$" in salary_summary and salary_period:
                salary_currency = "CAD"

        telework = _capture_text(_TELEWORK_RE, chunk)
        appmethod = _capture_text(_APPMETHOD_RE, chunk)
        is_remote = _infer_is_remote(telework, title)

        job_number = None
        jn = _JOB_NUMBER_RE.search(chunk)
        if jn:
            job_number = jn.group(1).strip()

        raw: dict[str, Any] = {"posted_text": date_raw} if date_raw else {}
        if telework:
            raw["telework"] = telework
        if appmethod:
            raw["application_method"] = appmethod
        if _POSTED_ON_JB_RE.search(chunk):
            raw["posted_on_job_bank"] = True
        if _NEW_FLAG_RE.search(chunk):
            raw["new_flag"] = True
        if job_number and job_number != ats_id:
            raw["jobbank_number"] = job_number

        country_iso = _infer_country_iso(location)
        language = self.language if self.language in {"en", "fr"} else "en"

        return Job(
            url=POSTING_URL_TEMPLATE.format(id=ats_id),
            title=title,
            company=company,
            ats_type=ATSType.JOBBANKCA,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="North America" if country_iso == "CA" else None,
            is_remote=is_remote,
            salary_summary=salary_summary,
            salary_currency=salary_currency,
            salary_period=salary_period,
            commitment=telework or None,
            requisition_id=job_number,
            posted_at=posted_at,
            fetched_at=fetched_at or datetime.now(tz=UTC),
            language=language,
            raw=raw or None,
        )


# --- module-level helpers ----------------------------------------------------


def _salary_period(summary: str | None) -> SalaryPeriod | None:
    if not summary:
        return None
    normalized = summary.casefold().replace("’", "'")
    markers: tuple[tuple[SalaryPeriod, tuple[str, ...]], ...] = (
        ("HOUR", ("hourly", "per hour", "par heure", "de l'heure", "à l'heure")),
        ("DAY", ("daily", "per day", "par jour")),
        ("WEEK", ("weekly", "per week", "par semaine")),
        ("MONTH", ("monthly", "per month", "par mois")),
        (
            "YEAR",
            ("annually", "annual", "yearly", "per year", "par année", "annuel"),
        ),
    )
    for period, candidates in markers:
        if any(candidate in normalized for candidate in candidates):
            return period
    return None


def _capture_text(pattern: re.Pattern[str], chunk: str) -> str | None:
    """Run ``pattern`` against ``chunk`` and return cleaned inner text.

    Strips nested tags, collapses whitespace, unescapes HTML entities.
    Returns ``None`` when the match is empty.
    """
    m = pattern.search(chunk)
    if not m:
        return None
    raw = m.group(1)
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def _parse_date(value: str | None) -> datetime | None:
    """Parse an English or French Job Bank publication date."""
    if not value:
        return None
    # Some listings include a trailing "Expires..." clause we don't
    # care about for posted_at.
    head = value.split("Expires", 1)[0].strip()
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", head)
    if m:
        month = _MONTHS_EN.get(m.group(1).lower())
        if month is not None:
            try:
                return datetime(
                    int(m.group(3)), month, int(m.group(2)), tzinfo=UTC,
                )
            except ValueError:
                return None
    french = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})", head)
    if not french:
        return None
    month = _MONTHS_FR.get(french.group(2).lower())
    if month is None:
        return None
    try:
        return datetime(
            int(french.group(3)), month, int(french.group(1)), tzinfo=UTC,
        )
    except ValueError:
        return None


def _infer_is_remote(telework: str | None, title: str) -> bool | None:
    """Set ``is_remote=True`` only when the source explicitly says so.

    Job Bank ships ``<span class="telework">`` with one of four
    labels: ``On site``, ``Hybrid``, ``Remote``, ``Telework``. We
    return ``True`` for the last two (telework is the older synonym),
    ``False`` only for the explicit ``On site``, and ``None`` for
    hybrid or missing flags so the downstream enrichment isn't
    forced to commit prematurely.
    """
    if telework:
        t = telework.strip().lower()
        if "remote" in t or "telework" in t or "télétravail" in t:
            return True
        if t == "on site" or "sur place" in t:
            return False
    # Title-only fallback mirrors the schema's documented behavior:
    # only ever assert True, never False.
    title_lc = title.lower()
    if "remote" in title_lc or "télétravail" in title_lc:
        return True
    return None


def _infer_country_iso(location: str | None) -> str | None:
    """Return ``"CA"`` when the location string contains a Canadian
    province / territory code or the word ``Canada``.

    Job Bank only posts Canadian jobs in practice, but we still gate on
    a visible signal so we don't claim country for, say, a freshly
    redesigned page where the location field is empty.
    """
    if not location:
        return "CA"  # Job Bank is Canada-only; default when unknown.
    if "Canada" in location:
        return "CA"
    for token in re.findall(r"\b([A-Z]{2})\b", location):
        if token in _CA_PROVINCES:
            return "CA"
    # Province inside parens, e.g. ``Burlington (ON)``.
    for token in re.findall(r"\(([A-Z]{2})\)", location):
        if token in _CA_PROVINCES:
            return "CA"
    return "CA"
