"""Rekrute.com — Morocco's largest dedicated job board.

Rekrute is the dominant general-purpose job board in Morocco, with
~1,700 active postings across every sector (finance, IT, healthcare,
industry, retail, …). Companies post directly through Rekrute's
recruiter dashboard — not a LinkedIn / Indeed syndication aggregator.

There is no public JSON API: the listing page is plain SSR HTML at
``https://www.rekrute.com/offres-emploi-maroc.html`` and the
``/offres.html`` search endpoint serves the same markup with ``?p=N``
pagination (10 jobs per page). Plain ``httpx`` clears the host
without any Cloudflare / Datadome challenge — we paginate with a
modest concurrency cap and parse each page's HTML directly.

Listing row shape (one ``<li class="post-id" id="…">`` per job):

    <li class="post-id" id="182716">
      <div>
        <div class="col-sm-2 …">
          <a href="/huir-emploi-recrutement-343150.html">
            <img alt="HUIR - L'Hôpital Universitaire …" …>
          </a>
        </div>
        <div class="col-sm-10 …">
          <h2><a class="titreJob" href="/offre-emploi-…-182716.html">
            Directeur (trice) de l'Hébergement | Rabat (Maroc)
          </a></h2>
          <div class="holder">
            <em class="date">… Publication : du <span>12/05/2026</span>
              au <span>12/07/2026</span></em>
            <ul>
              <li>Secteur d'activité : <a>Pharmacie / Santé</a></li>
              <li>Fonction : <a>Médical / Paramédical</a></li>
              <li>Expérience requise : <a>Expert (10 à 20 ans)</a></li>
              <li>Niveau d'étude demandé : <a>Bac +5 et plus</a></li>
              <li>Type de contrat proposé : <a>CDI</a>
                  - Télétravail : Non</li>
            </ul>
          </div>
        </div>
      </div>
    </li>

The ``id="…"`` attribute is Rekrute's internal numeric job id — we
use it as the canonical ``ats_id``. The recruiter's display name
comes from the logo anchor's ``alt`` attribute (or the link text on
the rare rows where the logo is missing). Locations are appended to
the title as ``"… | Ville (Maroc)"`` — we strip the trailing
``(Maroc)`` token and surface the city as ``location``.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / wanted / manfred pattern). All rows are
emitted with ``country_iso="MA"`` and ``region="Africa"``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

log = logging.getLogger(__name__)

# --- URL constants ----------------------------------------------------

SITE_BASE = "https://www.rekrute.com"
# ``/offres.html?p=N&s=1&o=1`` is what the pagination links resolve to;
# ``s=1`` selects "sorted by posting date desc" and ``o=1`` "office =
# any country" (Rekrute also surfaces a small International section
# we keep visible for completeness — country filtering lives in the
# scraper-side ``country_iso`` resolver, not the URL).
LISTING_URL_TEMPLATE = (
    "https://www.rekrute.com/offres.html?p={page}&s=1&o=1"
)

DEFAULT_MAX_PAGES = 250  # ~1.7k jobs / 10 per page → 170 normally.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# A real-browser UA is enough — Rekrute doesn't ship a bot challenge.
# We still set Accept-Language to French so the (occasionally
# bilingual) titles come back in their canonical FR form.
_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
}

# --- HTML parsing -----------------------------------------------------
#
# We use a tolerant regex to slice each ``<li class="post-id" id="…">``
# block out of the page (matching Bayt's approach) and then feed the
# slice into BeautifulSoup for structured field extraction. Trying to
# CSS-select the rows directly works too, but a regex slice halves
# the parse time on a 200KB page and keeps the per-row scopes small.
_ROW_BOUNDARY_RE = re.compile(
    r'<li\s+class="post-id"\s+id="(?P<id>\d+)"\s*>',
    re.IGNORECASE,
)
_RESULTS_LIST_RE = re.compile(
    r"<ul\b[^>]*\bid=['\"]post-data['\"][^>]*>",
    re.IGNORECASE,
)
_RESULTS_RANGE_RE = re.compile(
    r"<span\s+class=['\"]pages['\"]>\s*<span>\s*"
    r"(?P<start>[\d\s]+)\s*-\s*(?P<end>[\d\s]+)\s*</span>\s*"
    r"sur\s*(?P<total>[\d\s]+)",
    re.IGNORECASE,
)

# ``Publication : du <span>DD/MM/YYYY</span>`` — the first span in the
# ``<em class="date">`` block is always the publication date.
_DATE_DDMMYYYY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")

# Title suffix the listing appends after a vertical bar:
# ``"Job title here | Casablanca (Maroc)"`` → city is "Casablanca".
# ``"(Maroc)"`` is the canonical country suffix Rekrute uses;
# alternates like ``"(Tunisie)"`` show up rarely (the very small
# international section). We surface the country code from the
# suffix when we recognise it, otherwise default to MA.
_LOCATION_FROM_TITLE_RE = re.compile(
    r"\|\s*([^|]+?)\s*\(([^)]+)\)\s*$",
)

# Map title-suffix country names (FR) → ISO 3166-1 alpha-2. Rekrute
# is Morocco-centric so this stays small.
_TITLE_COUNTRY_TO_ISO: dict[str, str] = {
    "maroc": "MA",
    "tunisie": "TN",
    "algérie": "DZ",
    "algerie": "DZ",
    "france": "FR",
    "sénégal": "SN",
    "senegal": "SN",
    "côte d'ivoire": "CI",
    "cote d'ivoire": "CI",
    "espagne": "ES",
    "émirats arabes unis": "AE",
    "emirats arabes unis": "AE",
    "qatar": "QA",
    "arabie saoudite": "SA",
}

# ISO → continent. Morocco-centric so the table stays tight.
_ISO_TO_REGION: dict[str, str] = {
    "MA": "Africa", "TN": "Africa", "DZ": "Africa", "SN": "Africa",
    "CI": "Africa",
    "FR": "Europe", "ES": "Europe",
    "AE": "Asia", "QA": "Asia", "SA": "Asia",
}

# Translate Rekrute's French contract labels into the canonical
# normalised ``employment_type`` enum. Anything not in the map stays
# in ``commitment`` (the verbatim French label) — the canonical enum
# is a lossy normalisation, ``commitment`` preserves the original.
_CONTRACT_TO_EMPLOYMENT: dict[str, str] = {
    "cdi": "FULL_TIME",
    "cdd": "TEMPORARY",
    "freelance": "CONTRACT",
    "stage": "INTERN",
    "stage pfe": "INTERN",
    "intérim": "TEMPORARY",
    "interim": "TEMPORARY",
    "temps partiel": "PART_TIME",
}


@ScraperRegistry.register(ATSType.REKRUTE)
class RekruteScraper(BaseScraper):
    """Rekrute.com — Morocco's largest dedicated job board.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``max_pages``: safety cap. Rekrute normally pages out at ~170;
      keep some headroom for spikes.
    - ``concurrency``: parallel page fetches. Default 4 — the site
      tolerates more but we stay polite.
    """

    ats = ATSType.REKRUTE

    def __init__(
        self,
        company_slug: str = "any",
        *,
        timeout: float = 60.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self.concurrency = max(1, concurrency)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        fetched_at = datetime.now(tz=UTC)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            # We don't know the total page count up front so we walk
            # serially-with-a-window: keep ``concurrency`` requests
            # in flight, stop submitting once a page returns zero
            # rows. This avoids over-fetching at the tail (Rekrute's
            # last page is small) while staying mostly parallel.
            return await self._walk_pages(client, fetched_at)

    async def _walk_pages(
        self, client: httpx.AsyncClient, fetched_at: datetime,
    ) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        sem = asyncio.Semaphore(self.concurrency)
        stop_at_page: int | None = None
        skipped_fetch_pages: set[int] = set()
        skipped_parse_pages: set[int] = set()

        async def fetch_page(page_no: int) -> list[Job]:
            nonlocal stop_at_page
            async with sem:
                if stop_at_page is not None and page_no >= stop_at_page:
                    return []
                html_body = await self._get_listing_page(client, page_no)
            if html_body is None:
                skipped_fetch_pages.add(page_no)
                return []
            try:
                rows = list(_iter_rows(html_body))
            except _RowsParseError as exc:
                log.warning(
                    "Rekrute page=%d: %s — skipping page", page_no, exc,
                )
                skipped_parse_pages.add(page_no)
                return []
            if not rows:
                # First fully-empty page — mark it as the upper bound
                # so concurrent tasks at later pages bail out.
                if stop_at_page is None or page_no < stop_at_page:
                    stop_at_page = page_no
                return []
            page_jobs: list[Job] = []
            for row_id, row_html in rows:
                job = _parse_row(row_id, row_html, fetched_at=fetched_at)
                if job is not None:
                    page_jobs.append(job)
            return page_jobs

        # Submit pages in waves of ``concurrency`` so the early-stop
        # signal from a wave actually short-circuits the next wave.
        page_no = 1
        while page_no <= self.max_pages:
            wave_end = min(page_no + self.concurrency, self.max_pages + 1)
            results = await asyncio.gather(
                *(fetch_page(p) for p in range(page_no, wave_end))
            )
            wave_had_rows = False
            for page_jobs in results:
                if page_jobs:
                    wave_had_rows = True
                for job in page_jobs:
                    if job.ats_id is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
            wave_pages = range(page_no, wave_end)
            wave_had_skip = any(
                p in skipped_fetch_pages or p in skipped_parse_pages
                for p in wave_pages
            )
            if stop_at_page is not None or (
                not wave_had_rows and not wave_had_skip
            ):
                break
            page_no = wave_end
        log.info("Rekrute: fetched %d unique jobs", len(jobs))
        return jobs

    async def _get_listing_page(
        self, client: httpx.AsyncClient, page_no: int,
    ) -> str | None:
        """Fetch one listing page with retry on transient errors.

        Returns the HTML body on success, ``None`` to stop walking
        when terminal. The ``p`` index Rekrute uses is 0-based on the
        wire (``?p=0`` is page 1) — we keep callers 1-based here and
        translate at the URL boundary.
        """
        url = LISTING_URL_TEMPLATE.format(page=page_no - 1)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(url, headers=_HEADERS)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Rekrute page=%d transport error after %d retries: %s",
                        page_no, MAX_RETRIES, exc,
                    )
                    return None
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            status = response.status_code
            if status == 200:
                return response.text
            if status in (429,) or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Rekrute page=%d returned %d after %d retries — stopping",
                        page_no, status, MAX_RETRIES,
                    )
                    return None
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** (attempt - 1))
                )
                await asyncio.sleep(delay)
                continue
            log.warning(
                "Rekrute page=%d returned %d — stopping pagination",
                page_no, status,
            )
            return None


# --- module-level helpers ---------------------------------------------


class _RowsParseError(Exception):
    """Raised when a 200-OK page does not contain Rekrute's job list shell."""


def _iter_rows(html_body: str) -> Iterable[tuple[str, str]]:
    """Yield ``(post_id, row_html)`` for every job tile on the page.

    We slice from the start of one ``<li class="post-id">`` to the
    start of the next — the trailing slice for the last row is capped
    at 50 KB so we never pull in the page footer. Each row is ~2 KB
    in practice.
    """
    matches = list(_ROW_BOUNDARY_RE.finditer(html_body))
    if not matches and not _is_empty_results_page(html_body):
        raise _RowsParseError("job list shell not found")
    for idx, m in enumerate(matches):
        start = m.start()
        end = (
            matches[idx + 1].start() if idx + 1 < len(matches)
            else min(start + 50_000, len(html_body))
        )
        yield m.group("id"), html_body[start:end]


def _is_empty_results_page(html_body: str) -> bool:
    """Return true only for Rekrute's real empty results page.

    A shell-only 200 page with no ``post-id`` rows should be treated as
    malformed unless Rekrute's pagination says the requested range starts
    past the total result count. That keeps transient empty shells from
    looking like the end of pagination.
    """
    if _RESULTS_LIST_RE.search(html_body) is None:
        return False
    m = _RESULTS_RANGE_RE.search(html_body)
    if m is None:
        return False
    start = _parse_compact_int(m.group("start"))
    total = _parse_compact_int(m.group("total"))
    return total == 0 or start > total


def _parse_compact_int(value: str) -> int:
    digits = re.sub(r"\D+", "", value)
    return int(digits) if digits else 0


def _parse_row(
    row_id: str, row_html: str, *, fetched_at: datetime,
) -> Job | None:
    """Parse one ``<li class="post-id">`` into a Job. Returns ``None``
    when the row is missing the bare minimum (id + title + href)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(row_html, "html.parser")
    title_anchor = soup.find("a", class_="titreJob")
    if title_anchor is None:
        return None
    title_full = title_anchor.get_text(" ", strip=True)
    href = title_anchor.get("href")
    if not title_full or not href:
        return None
    url = _absolute_url(str(href))

    # Split title into ``title | location (country)``. Rekrute always
    # appends the city + country to the visible title.
    title, location, country_iso = _split_title(title_full)

    company = _extract_company(soup)
    description = _extract_description(soup)
    posted_at = _extract_posted_at(row_html)
    contract_label, employment_type, is_remote = _extract_contract(soup)
    sector, function = _extract_sector_function(soup)
    experience_label, study_level = _extract_experience_study(soup)

    raw: dict[str, Any] = {}
    if sector:
        raw["sector"] = sector
    if function:
        raw["function"] = function
    if experience_label:
        raw["experience_label"] = experience_label
    if study_level:
        raw["study_level"] = study_level
    if contract_label:
        raw["contract_label"] = contract_label

    return Job(
        url=url,
        title=title or title_full,
        company=company or "Unknown",
        ats_type=ATSType.REKRUTE,
        ats_id=row_id,
        location=location,
        country_iso=country_iso,
        region=_ISO_TO_REGION.get(country_iso or ""),
        is_remote=is_remote,
        employment_type=employment_type,  # type: ignore[arg-type]
        department=sector,
        commitment=contract_label,
        description=description,
        posted_at=posted_at,
        fetched_at=fetched_at,
        language="fr",
        raw=raw or None,
    )


def _absolute_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return SITE_BASE + path_or_url


def _split_title(title_full: str) -> tuple[str, str | None, str | None]:
    """Split ``"Job title | Casablanca (Maroc)"`` into
    ``("Job title", "Casablanca", "MA")``.

    Falls back to MA when the suffix is missing or unrecognized —
    Rekrute is a Morocco board, and known non-Morocco suffixes are
    mapped explicitly above.
    """
    m = _LOCATION_FROM_TITLE_RE.search(title_full)
    if m is None:
        # No "| City (Country)" suffix — keep the whole thing as the
        # title and assume MA (Rekrute's overwhelming majority).
        return title_full.strip(), None, "MA"
    city = m.group(1).strip()
    country_name = m.group(2).strip().lower()
    iso = _TITLE_COUNTRY_TO_ISO.get(country_name, "MA")
    title = title_full[: m.start()].strip()
    return title, city or None, iso


def _extract_company(soup: Any) -> str | None:
    """Recruiter name comes from logo metadata or the logo link text.

    Some rows omit a real logo image but keep the recruiter name as text
    in the same left-column anchor. Use that before falling back to
    ``Unknown`` at Job construction.
    """
    img = soup.find("img", class_="photo")
    if img is not None:
        alt = (img.get("alt") or img.get("title") or "").strip()
        if alt and not _is_placeholder_alt(alt):
            return alt
    for anchor in soup.select(".col-sm-2 a"):
        text = anchor.get_text(" ", strip=True)
        if text and not _is_placeholder_alt(text):
            return text
        label = (anchor.get("title") or "").strip()
        if label and not _is_placeholder_alt(label):
            return label
    return None


def _is_placeholder_alt(value: str) -> bool:
    """Rekrute occasionally serves a literal ``"Logo"`` / ``"photo"``
    placeholder when the recruiter hasn't uploaded a brand image. Skip
    those so we don't surface ``company="Logo"`` rows."""
    lowered = value.lower()
    return lowered in {"logo", "photo", "rekrute"}


def _extract_description(soup: Any) -> str | None:
    """The short blurb lives in ``<div class="info"><span>…</span></div>``
    at the top of the row body — Rekrute uses the ``info`` class for
    several blocks so we pick the *first* one that contains a span.
    """
    for div in soup.find_all("div", class_="info"):
        span = div.find("span")
        if span is not None:
            text = span.get_text(" ", strip=True)
            if text:
                return text
    return None


def _extract_posted_at(row_html: str) -> datetime | None:
    """Pull ``Publication : du DD/MM/YYYY au DD/MM/YYYY`` — the first
    date is the posting date. Defensive: if Rekrute changes the date
    format we get ``None`` rather than a crash."""
    # Anchor the search on the ``Publication`` token so we don't
    # accidentally pick a date from elsewhere on the row.
    pub_idx = row_html.find("Publication")
    if pub_idx == -1:
        return None
    m = _DATE_DDMMYYYY_RE.search(row_html, pub_idx)
    if m is None:
        return None
    day, month, year = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = "20" + year
    try:
        return datetime(int(year), int(month), int(day))
    except (ValueError, TypeError):
        return None


def _iter_facet_lis(soup: Any) -> Iterable[Any]:
    """Yield only the ``<li>`` elements inside the facet ``<ul>`` that
    holds the ``Secteur / Fonction / Expérience / Type de contrat``
    rows. The outer row container itself is a ``<li class="post-id">``
    so a naïve ``soup.find_all("li")`` would walk into it and grab
    the whole row's text — we scope tightly to the facet ``<ul>``
    inside the ``<div class="info">`` block."""
    for ul in soup.find_all("ul"):
        children = list(ul.find_all("li", recursive=False))
        if not children:
            continue
        # Heuristic: a facet ``<ul>`` is the one whose lis' joined text
        # mentions at least one canonical facet label. Avoids picking
        # an unrelated ``<ul>`` (nav, related-jobs widget, etc.).
        joined = " ".join(li.get_text(" ", strip=True) for li in children).lower()
        if any(
            token in joined for token in (
                "type de contrat", "secteur d'activité", "fonction",
                "expérience requise", "niveau d'étude",
            )
        ):
            yield from children
            return


def _extract_contract(soup: Any) -> tuple[str | None, str | None, bool | None]:
    """Parse the ``Type de contrat proposé : <a>CDI</a> - Télétravail : Non``
    row. Returns ``(raw_label, normalised_employment_type, is_remote)``.

    The remote flag is tri-state: ``True`` (Oui), ``False`` (Non), or
    ``None`` (not mentioned). We surface ``False`` explicitly when the
    site says so — that's signal too.
    """
    contract_label: str | None = None
    employment_type: str | None = None
    is_remote: bool | None = None
    for li in _iter_facet_lis(soup):
        text = li.get_text(" ", strip=True)
        lowered = text.lower()
        if "type de contrat" in lowered and contract_label is None:
            a = li.find("a")
            if a is not None:
                contract_label = a.get_text(strip=True) or None
            else:
                after = text.split(":", 1)[-1].strip()
                contract_label = after.split("-")[0].strip() or None
            if contract_label:
                employment_type = _CONTRACT_TO_EMPLOYMENT.get(
                    contract_label.lower().strip(),
                )
        if "télétravail" in lowered or "teletravail" in lowered:
            # Pull the value after the ``Télétravail`` label specifically —
            # the same ``<li>`` often holds ``Type de contrat proposé : CDI -
            # Télétravail : Non`` so a plain first-colon split would grab
            # the contract value instead.
            tt_match = re.search(
                r"t[ée]l[ée]travail\s*[:：]\s*([^|\-–]+)",
                text,
                re.IGNORECASE,
            )
            if tt_match:
                tt_value = tt_match.group(1).strip().lower()
                if (
                    tt_value.startswith("oui")
                    or "hybride" in tt_value
                    or "partiel" in tt_value
                ):
                    is_remote = True
                elif tt_value.startswith("non"):
                    is_remote = False
    return contract_label, employment_type, is_remote


def _extract_sector_function(soup: Any) -> tuple[str | None, str | None]:
    """Pull the ``Secteur d'activité`` and ``Fonction`` rows. Both
    are inline anchor labels and we keep the human-readable value."""
    sector: str | None = None
    function: str | None = None
    for li in _iter_facet_lis(soup):
        text = li.get_text(" ", strip=True)
        lowered = text.lower()
        if "secteur" in lowered and sector is None:
            sector = _value_after_colon(li, text)
        elif "fonction" in lowered and function is None:
            function = _value_after_colon(li, text)
    return sector, function


def _extract_experience_study(soup: Any) -> tuple[str | None, str | None]:
    """Pull the ``Expérience requise`` and ``Niveau d'étude`` rows.
    Stored verbatim in ``raw`` — we don't map to the canonical
    ``experience`` integer because Rekrute's labels are bucketed
    ranges (``"Expert (10 à 20 ans)"``) not single numbers."""
    experience: str | None = None
    study: str | None = None
    for li in _iter_facet_lis(soup):
        text = li.get_text(" ", strip=True)
        lowered = text.lower()
        if "expérience requise" in lowered and experience is None:
            experience = _value_after_colon(li, text)
        elif "niveau d'étude" in lowered and study is None:
            study = _value_after_colon(li, text)
    return experience, study


def _value_after_colon(li: Any, text: str) -> str | None:
    """Best-effort value extraction: prefer the ``<a>`` anchor's text
    (Rekrute wraps every value in a filter link), fall back to the
    substring after the first colon."""
    a = li.find("a")
    if a is not None:
        anchor_text = a.get_text(" ", strip=True)
        if anchor_text:
            return anchor_text
    if ":" in text:
        after = text.split(":", 1)[-1].strip()
        if after:
            return after
    return None


__all__ = [
    "RekruteScraper",
]
