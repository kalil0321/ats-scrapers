"""Bayt.com — the dominant job board across MENA / the Gulf.

Bayt.com is the largest job board in UAE, Saudi Arabia, Egypt, Lebanon,
Qatar, Kuwait, Bahrain, Oman, Jordan and Morocco — ~100k+ live
postings across the region depending on the day. There's no public
JSON API: the listings page itself is rendered server-side and the
SPA-style filtering is layered on top via plain ``?page=N`` query
strings. We scrape the HTML directly.

Bayt is **Cloudflare-protected** with a Turnstile challenge in front
of every page. Plain ``httpx`` / ``requests`` returns "Just a moment…"
with a 403. Reverse-engineered the bypass on 2026-05-12 via
``reverse-api-engineer`` + a JobSpy source cross-reference (the public
``cullenwatson/JobSpy`` and ``speedyapply/JobSpy`` projects both run a
single ``GET`` against the search URL and parse the HTML). The
challenge is occasionally satisfied by a real Chrome / Safari TLS
fingerprint via ``httpcloak`` (matching the Kariyer scraper's
PerimeterX bypass), but Cloudflare rotates the allowlist regularly so
we try multiple presets in rank order and fall back to graceful
degradation when none clears.

URL pattern: ``https://www.bayt.com/en/{country}/jobs/?page={N}``.
The country segment can be:

  - ``international`` — pan-MENA aggregate, default.
  - ``uae``, ``saudi-arabia``, ``egypt``, ``lebanon``, ``qatar``,
    ``kuwait``, ``bahrain``, ``oman``, ``jordan``, ``morocco`` —
    per-country slices.

HTML row shape (one ``<li data-js-job data-job-id="…">`` per job):

    <li data-js-job class="has-pointer-d" data-job-id="5223155">
      <h2>
        <a href="/en/saudi-arabia/jobs/it-audit-manager-5223155/">
          IT Audit Manager
        </a>
      </h2>
      <div class="t-nowrap p10l">
        <span class="t-default t-small">Michael Page</span>
      </div>
      <div class="t-mute t-small">Riyadh · Saudi Arabia</div>
      <div class="jb-descr m10t t-small">The IT Audit Manager will…</div>
      <dt class="p0 m20r jb-label-careerlevel">Management</dt>
      <span data-automation-jobactivedate="1734696141">52 minutes ago</span>
    </li>

We use ``data-job-id`` for the canonical ATS id (Bayt's internal job
id, not a hash) and the ``data-automation-jobactivedate`` Unix
timestamp for ``posted_at``. The ``href`` already encodes the country
slug so consumers can split ``country_iso`` out without an extra map
lookup — we still surface it directly for convenience.

Single-source scraper, multi-region: ``company_slug`` selects the
country segment. Pass ``"international"`` (default) for the MENA-wide
aggregate, or any of the per-country slugs above.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

# --- URL constants ----------------------------------------------------

_SITE_BASE = "https://www.bayt.com"
_LISTING_TEMPLATE = "https://www.bayt.com/en/{country}/jobs/?page={page}"

# Country slugs Bayt exposes. ``international`` is the MENA-wide
# aggregate — equivalent to no filter. The rest are per-country slices.
# The slug doubles as the URL segment.
_COUNTRY_SLUGS: dict[str, str | None] = {
    # slug → ISO 3166-1 alpha-2 (None for the multi-country
    # aggregate; downstream LLM enrichment derives per-row country
    # from the href in that case).
    "international": None,
    "uae": "AE",
    "saudi-arabia": "SA",
    "egypt": "EG",
    "lebanon": "LB",
    "qatar": "QA",
    "kuwait": "KW",
    "bahrain": "BH",
    "oman": "OM",
    "jordan": "JO",
    "morocco": "MA",
    # Alias for convenience — "saudi" is more common in everyday usage
    # than the full slug Bayt uses on the URL.
    "saudi": "SA",
}

# Map the ``Country`` portion of the location string (``City · Country``)
# to ISO 3166-1 alpha-2 when we're on the international aggregate. Keys
# are case-insensitive (we lower() before lookup).
_COUNTRY_NAME_TO_ISO: dict[str, str] = {
    "uae": "AE",
    "united arab emirates": "AE",
    "saudi arabia": "SA",
    "egypt": "EG",
    "lebanon": "LB",
    "qatar": "QA",
    "kuwait": "KW",
    "bahrain": "BH",
    "oman": "OM",
    "jordan": "JO",
    "morocco": "MA",
    "iraq": "IQ",
    "yemen": "YE",
    "syria": "SY",
    "palestine": "PS",
    "tunisia": "TN",
    "algeria": "DZ",
    "libya": "LY",
    "sudan": "SD",
}

# When we resolve the URL country segment (``/en/uae/…``) we get an
# ISO code directly — same alpha-2 codes as above, no mapping needed.
# Reverse-lookup mapping for slug → ISO:
_SLUG_TO_ISO: dict[str, str] = {
    k: v for k, v in _COUNTRY_SLUGS.items() if v is not None
}

# Default per-page count Bayt serves is 20–40 (varies by viewport).
# The catalogue is in the low six figures region-wide so an
# unrestricted walk needs ~5000 pages; the hard cap stops a runaway
# request loop if Bayt regresses on the "no more results" sentinel.
DEFAULT_MAX_PAGES = 500

# httpcloak retry knobs. Cloudflare 403s on the first request of a
# session are common — back off briefly and try again. After three
# failed attempts on page 1 we surface a ``ScraperError``; mid-pagination
# failures stop the loop and we keep whatever pages we already have.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# httpcloak TLS presets to try in order. Cloudflare's challenge
# matrix isn't deterministic; some IPs / TOD combinations clear with
# ``ios-safari-18`` (the Kariyer preset), others want a desktop
# fingerprint. We try the strongest first and fall through. Each
# preset uses a fresh ``Session`` so we don't carry a tainted cookie
# jar across attempts.
_HTTPCLOAK_PRESETS: tuple[str, ...] = (
    "chrome-latest-windows",
    "chrome-latest-macos",
    "safari-latest",
    "ios-safari-18",
    "firefox-latest",
)

# Headers a real Chromium-on-Windows browser sends. Cloudflare scores
# missing ``sec-ch-ua*`` / ``Sec-Fetch-*`` highly so we mirror them
# even though they're nominally optional. ``Accept-Language: en`` is
# what Bayt's English locale serves.
_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# --- HTML parsing regexes ---------------------------------------------
#
# Bayt's listing markup is fairly stable but the page also embeds
# ~100KB of unrelated chrome (nav, footer, related searches). We
# extract the ``<li data-js-job …>`` rows with a tolerant regex and
# then feed each body to BeautifulSoup for field extraction. The
# regex alone wouldn't be safe — nested ``<li>`` inside the
# description div would confuse a non-balanced matcher — so we cap
# each row at the *next* ``<li data-js-job`` boundary instead of
# trying to balance tags.

_LI_BOUNDARY_RE = re.compile(
    r'<li\s+data-js-job\b[^>]*data-job-id="(?P<id>[^"]+)"[^>]*>',
    re.IGNORECASE,
)
# Inside one row, the canonical fields are pulled by tightly-scoped
# CSS selectors via BeautifulSoup. We keep one regex for the unix
# timestamp because it appears as an attribute on a span and a
# strict regex is faster than soup-walking.
_JOB_DATE_TS_RE = re.compile(
    r'data-automation-jobactivedate="(\d+)"',
    re.IGNORECASE,
)

# Cloudflare's challenge page is identifiable by its title — we use
# this to distinguish "real" 200-status content from CF clearing
# pages that occasionally come back as 200 with a JS challenge body.
_CLOUDFLARE_TITLE = "<title>Just a moment..."


@ScraperRegistry.register(ATSType.BAYT)
class BaytScraper(BaseScraper):
    """Bayt.com MENA jobs scraper.

    Constructor knobs:
        company_slug: country segment. ``"international"`` (default,
            pan-MENA), or one of the per-country slugs in
            :data:`_COUNTRY_SLUGS`. Aliases (``"saudi"``) are accepted
            but the canonical slug from ``_COUNTRY_SLUGS`` is used on
            the URL.
        max_pages: stop after this many pages even if more remain.

    The scraper depends on the optional ``httpcloak`` extra. When the
    library isn't installed (``pip install jobhive`` without the
    ``[scrapers]`` extra) it logs a warning and returns ``[]`` — same
    optional-fallback contract as Tesla / Kariyer.
    """

    ats = ATSType.BAYT

    def __init__(
        self,
        company_slug: str = "international",
        *,
        timeout: float = 60.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        # Normalize the slug to one Bayt's URLs understand. ``saudi``
        # is mapped to ``saudi-arabia`` because that's the URL form
        # the site canonicalises to.
        normalized = (company_slug or "international").lower().strip()
        if normalized == "saudi":
            normalized = "saudi-arabia"
        if normalized not in _COUNTRY_SLUGS:
            raise ScraperError(
                f"Bayt unknown country slug {company_slug!r}. "
                f"Known: {sorted(_COUNTRY_SLUGS)}"
            )
        super().__init__(normalized, timeout=timeout)
        self.country_slug = normalized
        self.country_iso_hint = _COUNTRY_SLUGS[normalized]
        self.max_pages = max_pages

    # ----- public entry point -----------------------------------------

    def fetch(self) -> list[Job]:
        if not _httpcloak_available():
            _warn_httpcloak_disabled()
            return []
        return list(self._fetch_via_httpcloak())

    # ----- core fetch loop --------------------------------------------

    def _fetch_via_httpcloak(self) -> Iterable[Job]:
        """Paginate the listing pages until Bayt stops returning new
        rows. We dedupe by ``data-job-id`` because the "Featured" /
        sticky rows recur on every page.
        """
        fetched_at = datetime.now(tz=UTC)
        seen: set[str] = set()
        jobs: list[Job] = []
        first_page_html: str | None = None
        # Try presets in order; the first one that gets a non-CF
        # response on page 1 wins. The rest of the pagination keeps
        # that same session (TLS fingerprint reuse minimises the
        # chance Cloudflare rotates us out mid-walk).
        session, first_page_html = self._open_session_and_first_page()
        if session is None or first_page_html is None:
            # All presets blocked. We've already logged a warning;
            # graceful degradation per the optional-fallback contract.
            return jobs
        try:
            for page_no in range(1, self.max_pages + 1):
                if page_no == 1:
                    page_html: str | None = first_page_html
                else:
                    page_html = self._fetch_page(session, page_no)
                if page_html is None:
                    break
                page_items = list(_iter_job_rows(page_html))
                if not page_items:
                    break
                new_in_page = 0
                for row_id, row_html in page_items:
                    if row_id in seen:
                        continue
                    seen.add(row_id)
                    job = self._parse_row(
                        row_id, row_html, fetched_at=fetched_at,
                    )
                    if job is not None:
                        jobs.append(job)
                        new_in_page += 1
                if new_in_page == 0:
                    # All rows on this page were already seen (sticky
                    # tail) — stop walking before we burn budget on
                    # repeats.
                    break
        finally:
            # ``httpcloak.Session`` exposes ``close()``; mirror
            # contextlib semantics manually because we don't open it
            # inside a ``with`` block (the first-page open lives in
            # a helper and we keep it alive across pages).
            with _SuppressedClose():
                session.close()
        log.info(
            "Bayt %s: fetched %d unique jobs across up to %d pages",
            self.country_slug, len(jobs), self.max_pages,
        )
        return jobs

    def _open_session_and_first_page(self) -> tuple[Any, str | None]:
        """Try each ``httpcloak`` TLS preset in order until one gets a
        non-Cloudflare-challenge page 1. Returns the open session +
        the first page HTML on success; ``(None, None)`` on failure.

        We don't raise on every-preset-failure — Cloudflare's pattern
        is to block hard for a window then ease up, so a missed
        scrape is preferable to crashing the publish pipeline.
        """
        import httpcloak

        last_status: int | None = None
        for preset in _HTTPCLOAK_PRESETS:
            session = httpcloak.Session(preset=preset)
            url = _build_url(self.country_slug, 1)
            try:
                response = session.get(
                    url, headers=_HEADERS, timeout=self.timeout,
                )
            except httpcloak.HTTPCloakError as exc:
                log.warning(
                    "Bayt: preset %s transport error: %s", preset, exc,
                )
                with _SuppressedClose():
                    session.close()
                continue

            last_status = response.status_code
            text = response.text
            if response.status_code == 200 and _CLOUDFLARE_TITLE not in text:
                log.info(
                    "Bayt: preset %s cleared Cloudflare", preset,
                )
                return session, text
            log.info(
                "Bayt: preset %s blocked (status=%d, cf=%s)",
                preset, response.status_code, _CLOUDFLARE_TITLE in text,
            )
            with _SuppressedClose():
                session.close()

        log.warning(
            "Bayt: all %d TLS presets blocked by Cloudflare "
            "(last status=%s) — skipping. Try a residential proxy "
            "or cloakbrowser if this persists.",
            len(_HTTPCLOAK_PRESETS), last_status,
        )
        return None, None

    def _fetch_page(self, session: Any, page_no: int) -> str | None:
        """GET one listing page with retry on 429 / 5xx / transient
        Cloudflare 403. Returns ``None`` to break the pagination loop
        when terminal."""
        import httpcloak

        url = _build_url(self.country_slug, page_no)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = session.get(
                    url, headers=_HEADERS, timeout=self.timeout,
                )
            except httpcloak.HTTPCloakError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Bayt %s page=%d transport error after %d retries: %s",
                        self.country_slug, page_no, MAX_RETRIES, exc,
                    )
                    return None
                _sleep_backoff(attempt)
                continue

            status = response.status_code
            text = response.text
            if status == 200 and _CLOUDFLARE_TITLE not in text:
                return text
            if status in (403, 429) or 500 <= status < 600:
                # Cloudflare flipped the bot manager back on, or the
                # gateway hiccuped. Retry with backoff.
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Bayt %s page=%d returned %d (cf=%s) after "
                        "%d retries — stopping pagination",
                        self.country_slug, page_no, status,
                        _CLOUDFLARE_TITLE in text, MAX_RETRIES,
                    )
                    return None
                _sleep_backoff(attempt)
                continue
            # Some other 4xx — surface so an operator notices.
            log.warning(
                "Bayt %s page=%d returned unexpected status %d — stopping",
                self.country_slug, page_no, status,
            )
            return None

        log.warning(
            "Bayt %s page=%d exhausted retries: %s",
            self.country_slug, page_no, last_exc,
        )
        return None

    # ----- single-row parser ------------------------------------------

    def _parse_row(
        self,
        row_id: str,
        row_html: str,
        *,
        fetched_at: datetime,
    ) -> Job | None:
        """Parse one ``<li data-js-job>`` row body into a Job.

        Returns ``None`` when the row is missing the bare minimum
        (id + title + href). Bayt occasionally surfaces stub rows for
        confidential postings — skipping is preferable to fabricating
        values.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(row_html, "html.parser")
        h2 = soup.find("h2")
        if h2 is None:
            return None
        title_anchor = h2.find("a")
        if title_anchor is None:
            return None
        title = title_anchor.get_text(strip=True)
        href = title_anchor.get("href")
        if not title or not href:
            return None

        url = _absolute_url(str(href))

        # Company — the ATS wraps it in
        # ``<div class="t-nowrap p10l">…<span>Name</span>…</div>``.
        company: str | None = None
        company_block = soup.find("div", class_="t-nowrap p10l")
        if company_block is not None:
            company_span = company_block.find("span")
            if company_span is not None:
                company = company_span.get_text(strip=True) or None

        # Location — ``<div class="t-mute t-small">City · Country</div>``.
        # On detail-rich rows the same class is also used for "X hours
        # ago" footer text; we anchor on the *first* match which is
        # always the location.
        location: str | None = None
        for div in soup.find_all("div", class_="t-mute t-small"):
            text = div.get_text(strip=True)
            if text:
                location = text
                break

        country_iso = _resolve_country_iso(
            self.country_iso_hint, str(href), location,
        )

        # Description preview (~200 chars) — Bayt prepends an ellipsis
        # variant so we strip it. Useful as a coarse search signal
        # before the LLM enrichment fills in the full body.
        description: str | None = None
        desc_div = soup.find("div", class_="jb-descr")
        if desc_div is not None:
            description = desc_div.get_text(strip=True) or None

        # Career level / department surrogate — pulled from
        # ``<dt class="… jb-label-careerlevel">Management</dt>``.
        career_level: str | None = None
        career_dt = soup.find("dt", class_="jb-label-careerlevel")
        if career_dt is not None:
            career_level = career_dt.get_text(strip=True) or None

        # Posted-at timestamp — Unix epoch seconds in the
        # ``data-automation-jobactivedate`` attribute. We use the
        # original ``row_html`` (not the soup) because the regex is
        # ~10x faster than walking spans.
        posted_at: datetime | None = None
        ts_match = _JOB_DATE_TS_RE.search(row_html)
        if ts_match:
            try:
                posted_at = datetime.fromtimestamp(
                    int(ts_match.group(1)), tz=UTC,
                )
            except (ValueError, OSError, OverflowError):
                posted_at = None

        # External-applicant signal: Bayt marks aggregated /
        # external postings with ``data-automation-is_aggregated`` /
        # ``data-automation-is_external`` on the title anchor. We
        # surface as ``raw`` overflow for the publish layer to use.
        is_aggregated = (
            title_anchor.get("data-automation-is_aggregated") == "1"
        )
        is_external = (
            title_anchor.get("data-automation-is_external") == "1"
        )

        raw_overflow: dict[str, object] = {
            "career_level": career_level,
            "country_slug": self.country_slug,
            "is_aggregated": is_aggregated,
            "is_external": is_external,
        }

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.BAYT,
            ats_id=row_id,
            location=location,
            country_iso=country_iso,
            region=_region_for_iso(country_iso),
            posted_at=posted_at,
            fetched_at=fetched_at,
            language="en",
            description=description,
            raw=raw_overflow,
        )


# --- module-level helpers ---------------------------------------------


def _httpcloak_available() -> bool:
    """Return True if ``httpcloak`` is importable.

    Mirrors the optional-browser-fallback contract used by Kariyer /
    Tesla — when the dependency is missing the scraper logs a warning
    and returns ``[]`` instead of crashing.
    """
    try:
        import httpcloak  # noqa: F401
    except ImportError:
        return False
    return True


def _warn_httpcloak_disabled() -> None:
    log.warning(
        "Bayt: httpcloak required to bypass Cloudflare Turnstile — "
        "install with `pip install jobhive[scrapers]`. Skipping.",
    )


def _build_url(country_slug: str, page: int) -> str:
    """Compose the listing URL. ``saudi`` is canonicalised upstream so
    no aliasing is needed here."""
    return _LISTING_TEMPLATE.format(country=country_slug, page=page)


def _absolute_url(path_or_url: str) -> str:
    """Bayt ``href``s are site-relative; resolve to the absolute URL
    the public dataset stores."""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return _SITE_BASE + path_or_url


def _iter_job_rows(html_body: str) -> Iterable[tuple[str, str]]:
    """Yield ``(job_id, row_html)`` for every ``<li data-js-job>`` on
    the page. We use the *next* ``<li data-js-job`` boundary as the
    end-of-row marker; nested ``<li>``-like markup inside the
    description div would confuse a balanced-tag matcher but our
    scoped slice is robust to it because Bayt never nests one job
    listing inside another.
    """
    matches = list(_LI_BOUNDARY_RE.finditer(html_body))
    for idx, m in enumerate(matches):
        start = m.start()
        # End is the next row's start; for the last row we cap at
        # 50 KB downstream to avoid pulling the footer in. Bayt rows
        # are 1–3 KB in practice.
        end = matches[idx + 1].start() if idx + 1 < len(matches) else start + 50_000
        yield m.group("id"), html_body[start:end]


def _resolve_country_iso(
    iso_hint: str | None,
    href: str,
    location_text: str | None,
) -> str | None:
    """Best-effort ISO 3166-1 alpha-2 from the row.

    Order of precedence:
      1. The scraper's country hint (set when ``company_slug`` is a
         per-country slug, e.g. ``uae`` → ``AE``).
      2. The href country segment (``/en/{country}/jobs/…``).
      3. The trailing token of ``location_text`` (``City · Country``).

    Returns ``None`` if none match — downstream LLM enrichment fills
    it from the location string.
    """
    if iso_hint is not None:
        return iso_hint
    # Parse ``/en/saudi-arabia/jobs/…`` → ``saudi-arabia`` → ``SA``.
    m = re.match(r"^/?en/([a-z-]+)/jobs/", href.lower())
    if m:
        slug = m.group(1)
        if slug in _SLUG_TO_ISO:
            return _SLUG_TO_ISO[slug]
    # Last resort — split the location at the centre-dot.
    if location_text:
        parts = re.split(r"[·•]", location_text)
        if len(parts) >= 2:
            country_name = parts[-1].strip().lower()
            if country_name in _COUNTRY_NAME_TO_ISO:
                return _COUNTRY_NAME_TO_ISO[country_name]
    return None


# ISO → continent. MENA spans Asia (Gulf states) and Africa (North
# African states). Continent-level coarseness matches the rest of the
# codebase (see Kariyer for the same idea).
_ISO_TO_REGION: dict[str, str] = {
    "AE": "Asia", "SA": "Asia", "LB": "Asia", "QA": "Asia",
    "KW": "Asia", "BH": "Asia", "OM": "Asia", "JO": "Asia",
    "IQ": "Asia", "YE": "Asia", "SY": "Asia", "PS": "Asia",
    "EG": "Africa", "MA": "Africa", "TN": "Africa",
    "DZ": "Africa", "LY": "Africa", "SD": "Africa",
}


def _region_for_iso(iso: str | None) -> str | None:
    if iso is None:
        return None
    return _ISO_TO_REGION.get(iso)


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff for transient transport / 429 errors.

    Factored out so tests can monkey-patch a no-op replacement and
    not pay the wall-clock penalty on every retry path.
    """
    import time

    time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))


class _SuppressedClose:
    """Context manager that swallows any exception raised inside.

    Used around ``session.close()`` calls so a transport-layer
    cleanup failure can't mask the real reason we were closing the
    session in the first place. Equivalent to
    ``contextlib.suppress(Exception)`` but avoids the indirection
    (the same try/except shows up across half a dozen scrapers).
    """

    def __enter__(self) -> _SuppressedClose:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)
