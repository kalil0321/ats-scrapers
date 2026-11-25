"""InfoJobs Spain (https://www.infojobs.net) — Spain's #1 job board scraper.

InfoJobs Spain (``infojobs.net``) is the largest direct-posting jobs
board in Spain: ~59,000 live postings as of 2026-11 with 20 cards
per listing page (plus 2 promoted ones on page 1), so ~2,950 pages
cover the whole catalogue; the page after the last one is empty.
Companies post directly, not aggregated from LinkedIn / Indeed.

Distinct platform from **InfoJobs Brasil** (``infojobs.com.br``): same
brand, completely different domain, HTML structure, employment-type
labels, and currency. The two scrapers share nothing other than the
brand prefix on the module name.

The Spanish site is a React SPA whose listing pages embed the entire
search payload in ``window.__INITIAL_PROPS__ = JSON.parse("…");`` —
nested twice-encoded JSON. Once decoded, ``offers[]`` carries 22
structured records per page with every field we need:

  - ``code``         → ``ats_id`` (deterministic 30-char hex hash)
  - ``link``         → detail URL (relative, protocol-less)
  - ``title``        → ``title``
  - ``description``  → ``description`` (already plain text)
  - ``city``         → ``location``
  - ``companyName``  → ``company``
  - ``contractType`` → Spanish label, mapped to ``employment_type`` + ``commitment``
  - ``workday``      → working-hours label, kept in ``raw["workday"]``
  - ``teleworking``  → "Presencial" / "Híbrido" / "Remoto" / null → ``is_remote``
  - ``publishedAt``  → ``posted_at`` (ISO-8601 with ``Z``)
  - ``salary``       → ``{range:{min,max}, period, currency, type}`` (often absent)

The listing host sits behind Imperva bot protection (``reese84`` cookie,
Geetest captcha). The pages are read in up to three ways, cheapest first:

1. ``httpcloak`` (browser TLS fingerprint). An address without the
   ``reese84`` cookie gets about five pages, then a "No podemos
   identificar tu navegador" page until the next day.
2. ``cloakbrowser`` (local stealth Chromium). Its first page runs the
   Imperva script and earns the cookie; the remaining pages are fetched
   from inside that page, about one every 1.7 s. Faster than about one a
   second, Imperva answers 405 with a captcha, which clears after a pause.
3. The ``cloud_browser`` given to the scraper, same flow, when
   cloakbrowser is missing or refused.

Each step starts at the page the previous one stopped at. A read that
stops before the end of the catalogue raises :class:`ScraperError`: a
partial catalogue is never returned as a complete one.

Pagination is via ``?page=N`` (1-indexed). The
``overview.totalElements`` field on each page lets us stop early once
we've collected the entire catalogue.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ats_scrapers.cloud_browser import CDPCloudBrowser
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import proxy_url_from_env
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import _cloakbrowser as cb
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from ats_scrapers.models import EngagementType

API_ROOT = "https://www.infojobs.net"
LISTING_URL = f"{API_ROOT}/ofertas-trabajo"
DEFAULT_MAX_PAGES = 3500  # ~59k offers / 20 per page = ~2,950 pages
# Retry knobs for the httpcloak transport. This scraper deliberately
# does NOT use the shared Fetcher's cloak engine: InfoJobs treats 403
# as *transient* (Distil challenges deep pagination and backs off),
# which the Fetcher never retries in cloak mode.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Pause after each page read in a browser. Measured from one address on
# 25 Nov 2026: one page every 1.5 s read 80 pages without a captcha, one a
# second drew a captcha about every 30 pages.
BROWSER_PAGE_DELAY = 1.2
# Waits before a refused page is read again in the browser. A 30 s pause
# cleared every captcha seen while measuring.
BROWSER_BLOCK_WAITS = (30.0, 60.0, 120.0)
# How long the first page may take to run the Imperva script and reload.
BROWSER_CHALLENGE_TIMEOUT = 30.0
_FETCH_IN_PAGE_JS = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    return {status: r.status, body: await r.text()};
}"""

# Spanish-Spain employment-type labels → canonical EmploymentType enum.
# Keys are the literal strings InfoJobs Spain ships on ``contractType``.
# Any new label surfaces as None (logged once via _log_unknown) rather
# than being silently mis-classified.
_CONTRACT_TYPE_MAP: dict[str, str] = {
    "Contrato indefinido": "FULL_TIME",
    "Indefinido": "FULL_TIME",
    "Contrato de duración determinada": "TEMPORARY",
    "Duración determinada": "TEMPORARY",
    "Contrato fijo discontinuo": "TEMPORARY",
    "Fijo discontinuo": "TEMPORARY",
    "Contrato de prácticas": "INTERN",
    "Prácticas": "INTERN",
    "Becario": "INTERN",
    "De formación": "INTERN",
    "Contrato formativo": "INTERN",
    "Contrato autónomo": "CONTRACT",
    "Autónomo": "CONTRACT",
    "Otros contratos": "CONTRACT",
    "Otro tipo de contrato": "CONTRACT",
    "A tiempo parcial": "PART_TIME",
}

_CONTRACT_ENGAGEMENT: dict[str, EngagementType] = {
    "indefinido": "permanent",
    "fijo discontinuo": "permanent",
    "duración determinada": "fixed_term",
    "prácticas": "internship",
    "autónomo": "independent_contractor",
}

# Working-method label → is_remote. "Remoto" / "100% remoto" → fully
# remote; "Híbrido" → False (role isn't fully remote); "Presencial" →
# False; null/missing → None (don't infer). Keep the strings verbatim
# in ``raw["modality"]`` so consumers can recover the original signal.
_REMOTE_TELEWORKING = {"Remoto", "100% remoto", "Teletrabajo", "Remote"}
_ONSITE_TELEWORKING = {"Presencial", "Híbrido", "Hybrid", "Mixto"}

# The hydration payload lives inside ``window.__INITIAL_PROPS__ =
# JSON.parse("…");`` — a JS string literal containing a JSON document.
# We extract the string literal with a balanced walker (regex won't
# do; the payload contains escaped quotes), then ``json.loads`` twice.
_INITIAL_PROPS_MARKER = "window.__INITIAL_PROPS__"
_JSON_PARSE_OPEN = 'JSON.parse('


@ScraperRegistry.register(ATSType.INFOJOBSES)
class InfoJobsSpainScraper(BaseScraper):
    """InfoJobs Spain (infojobs.net) — Spain's #1 direct-posting jobs board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"spain"``) — the scraper paginates the entire
    catalogue.

    Knobs:

    - ``max_pages`` — pagination cap (default 3500 → roughly the full
      ~59k-offer catalogue with headroom).
    - ``listing_url`` — override the base URL when restricting to a
      city / category. ``?page=N`` is appended by the scraper; don't
      include it in the override.
    - ``cloud_browser``: hosted browser for the pages cloakbrowser
      cannot read.
    """

    ats = ATSType.INFOJOBSES

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        listing_url: str = LISTING_URL,
        cloud_browser: CDPCloudBrowser | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if cloud_browser is not None and not isinstance(cloud_browser, CDPCloudBrowser):
            raise TypeError(
                "cloud_browser must be a CDPCloudBrowser (InfoJobs Spain drives the page), "
                f"not {type(cloud_browser).__name__}"
            )
        self.max_pages = max(1, max_pages)
        self.listing_url = listing_url
        self.cloud_browser = cloud_browser

    async def afetch(self) -> list[Job]:
        from importlib.util import find_spec

        has_httpcloak = find_spec("httpcloak") is not None
        has_browser = cb.is_enabled() or self.cloud_browser is not None
        if not has_httpcloak and not has_browser:
            log.warning(
                "InfoJobs Spain: httpcloak or cloakbrowser is required to get "
                "past the bot protection. Install with "
                "`pip install ats-scrapers[scrapers]`. Skipping (returning [])."
            )
            return []

        state = _Catalogue(max_pages=self.max_pages)
        if has_httpcloak:
            try:
                await self._paginate(state, self._fetch_page)
                return state.jobs
            except ScraperError as exc:
                if not has_browser:
                    raise _stopped(state, exc) from exc
                log.info(
                    "InfoJobs Spain: plain requests stopped at page %d (%s); "
                    "reading on in a browser.",
                    state.page, exc,
                )
        await self._read_in_browser(state)
        return state.jobs

    async def _paginate(
        self,
        state: _Catalogue,
        read: Callable[[int], Awaitable[dict[str, Any]]],
    ) -> None:
        """Read pages from ``state.page`` on. A failed read raises and
        leaves ``state.page`` on the page to read next."""
        while not state.finished:
            payload = await read(state.page)
            state.add_page(payload, self._parse_offer)

    async def _read_in_browser(self, state: _Catalogue) -> None:
        if cb.is_enabled():
            try:
                await self._read_in_cloakbrowser(state)
                return
            except Exception as exc:
                if self.cloud_browser is None:
                    raise _stopped(state, exc) from exc
                log.warning(
                    "InfoJobs Spain: cloakbrowser stopped at page %d (%s: %s); "
                    "reading on in the cloud browser.",
                    state.page, type(exc).__name__, str(exc)[:200],
                )
        if self.cloud_browser is None:
            raise _stopped(state, ScraperError("no browser to read on with"))
        cloud_browser = self.cloud_browser
        while True:
            start = state.page
            try:
                await cloud_browser.run_page(
                    lambda page: self._paginate_in_page(page, state)
                )
                return
            except Exception as exc:
                # Hosted sessions end after a set time: a session that read
                # pages is replaced and the read goes on.
                if state.page == start:
                    raise _stopped(state, exc) from exc
                log.warning(
                    "InfoJobs Spain: cloud browser stopped at page %d (%s: %s); "
                    "opening another session.",
                    state.page, type(exc).__name__, str(exc)[:200],
                )

    async def _read_in_cloakbrowser(self, state: _Catalogue) -> None:
        from cloakbrowser import launch_async

        proxy = self.proxy or proxy_url_from_env()
        browser = await launch_async(
            headless=True, humanize=True, **({"proxy": proxy} if proxy else {}),
        )
        try:
            await self._paginate_in_page(await browser.new_page(), state)
        finally:
            await browser.close()

    async def _paginate_in_page(self, page: Any, state: _Catalogue) -> None:
        """Open the listing in ``page`` so Imperva hands it a cookie, then
        fetch the remaining pages from inside it."""
        await self._clear_challenge(page, _page_url(self.listing_url, state.page))

        async def read(number: int) -> dict[str, Any]:
            url = _page_url(self.listing_url, number)
            waits = iter(BROWSER_BLOCK_WAITS)
            while True:
                answer = await page.evaluate(_FETCH_IN_PAGE_JS, url)
                status, body = answer.get("status"), answer.get("body") or ""
                if status == 200 and _INITIAL_PROPS_MARKER in body:
                    await asyncio.sleep(BROWSER_PAGE_DELAY)
                    return _extract_initial_props(body)
                server_error = isinstance(status, int) and status >= 500
                cause = (
                    f"InfoJobs Spain returned {status} for {url}" if server_error
                    else f"InfoJobs Spain: captcha-gated page ({status}) for {url}"
                )
                wait = next(waits, None)
                if wait is None:
                    raise ScraperError(f"{cause} after {len(BROWSER_BLOCK_WAITS)} waits")
                log.info("%s; reading it again in %.0f s.", cause, wait)
                await asyncio.sleep(wait)

        await self._paginate(state, read)

    async def _clear_challenge(self, page: Any, url: str) -> None:
        """Load ``url`` until the listing shows. The Imperva script runs on
        the first answer and reloads the page by itself."""
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        deadline = time.monotonic() + BROWSER_CHALLENGE_TIMEOUT
        while True:
            try:
                html = await page.content()
            except Exception:
                # The page is reloading after the challenge.
                html = ""
            if _INITIAL_PROPS_MARKER in html:
                return
            if time.monotonic() >= deadline:
                raise ScraperError(
                    f"InfoJobs Spain: the browser challenge did not clear for {url} "
                    "(captcha-gated)"
                )
            await asyncio.sleep(0.5)

    async def _fetch_page(self, page: int) -> dict[str, Any]:
        url = _page_url(self.listing_url, page)
        text = await self._request_via_httpcloak(url)
        return _extract_initial_props(text)

    async def _request_via_httpcloak(self, url: str) -> str:
        """Fetch via httpcloak with retry/backoff. Returns the HTML
        body on 200, raises :class:`ScraperError` on hard failures.

        429 and 5xx are retried with exponential backoff; 403 (when
        even httpcloak gets challenged on deep pagination) is treated
        as transient up to ``MAX_RETRIES``."""
        last_status: int | None = None
        # Explicit ctor proxy wins over the env-derived one.
        proxy = self.proxy or proxy_url_from_env()
        for attempt in range(1, MAX_RETRIES + 1):
            result = await asyncio.to_thread(
                _httpcloak_get_sync, url, self.timeout, proxy
            )
            if isinstance(result, str):
                return result
            last_status = result
            if last_status not in (403, 429) and not (500 <= last_status < 600):
                raise ScraperError(
                    f"InfoJobs Spain returned {last_status} for {url}"
                )
            if attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        raise ScraperError(
            f"InfoJobs Spain returned {last_status} for {url} "
            f"after {MAX_RETRIES} retries"
        )

    def _parse_offer(self, offer: dict[str, Any]) -> Job | None:
        code = offer.get("code")
        title = offer.get("title")
        link = offer.get("link")
        if not (isinstance(code, str) and code and isinstance(title, str)
                and title and isinstance(link, str) and link):
            return None

        url = _absolutize_link(link)
        company = (offer.get("companyName") or "").strip() or "Empresa confidencial"
        location = offer.get("city") or None
        if isinstance(location, str):
            location = location.strip() or None

        contract_type_raw = offer.get("contractType")
        contract_label = (
            contract_type_raw.casefold().removeprefix("contrato ").removeprefix("de ")
            if isinstance(contract_type_raw, str) else ""
        )
        employment_type = (
            _CONTRACT_TYPE_MAP.get(contract_type_raw)
            if isinstance(contract_type_raw, str)
            else None
        )

        teleworking_raw = offer.get("teleworking")
        is_remote = _infer_remote(teleworking_raw)

        (salary_min, salary_max, salary_currency, salary_period,
         salary_summary) = _parse_salary(offer.get("salary"))

        posted_at = _parse_published_at(offer.get("publishedAt"))

        description = offer.get("description") or None
        if isinstance(description, str):
            description = description.strip() or None

        workday = offer.get("workday")
        raw: dict[str, Any] = {}
        if isinstance(workday, str) and workday:
            raw["workday"] = workday
        if isinstance(teleworking_raw, str) and teleworking_raw:
            raw["modality"] = teleworking_raw
        if isinstance(contract_type_raw, str) and contract_type_raw:
            raw["contract_type"] = contract_type_raw
        company_link = offer.get("companyLink")
        if isinstance(company_link, str) and company_link:
            raw["company_link"] = company_link
        if offer.get("executive") is True:
            raw["executive"] = True

        return Job(
            url=url,
            title=title.strip(),
            company=company,
            ats_type=ATSType.INFOJOBSES,
            ats_id=code,
            location=location,
            country_iso="ES",
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            engagement_type=_CONTRACT_ENGAGEMENT.get(contract_label),
            commitment=contract_type_raw if isinstance(contract_type_raw, str) else None,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language="es",
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


@dataclass
class _Catalogue:
    """What the reads gathered so far. Each way of reading picks up at
    ``page``."""

    max_pages: int
    page: int = 1
    jobs: list[Job] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    total: int | None = None
    consecutive_empty: int = 0
    complete: bool = False

    @property
    def finished(self) -> bool:
        return self.complete or self.page > self.max_pages or self.consecutive_empty >= 3

    def add_page(
        self,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], Job | None],
    ) -> None:
        offers = payload.get("offers") or []
        if self.total is None:
            total = (payload.get("overview") or {}).get("totalElements")
            if isinstance(total, int) and total >= 0:
                self.total = total
        new = 0
        for offer in offers:
            job = parse(offer)
            if job is None or job.ats_id in self.seen:
                continue
            self.seen.add(job.ats_id)
            self.jobs.append(job)
            new += 1
        self.consecutive_empty = 0 if new else self.consecutive_empty + 1
        # ``totalElements`` is counted when the read starts: postings
        # published during the read leave a gap the empty-page count closes.
        if self.total is not None and len(self.jobs) >= self.total:
            self.complete = True
        self.page += 1


def _stopped(state: _Catalogue, cause: BaseException) -> ScraperError:
    total = f"{state.total:,}" if state.total is not None else "an unknown number of"
    return ScraperError(
        f"InfoJobs Spain: stopped at page {state.page} after {len(state.jobs):,} "
        f"of {total} jobs: {cause}"
    )


def _httpcloak_get_sync(
    url: str, timeout: float, proxy: str | None = None
) -> str | int:
    """Sync ``httpcloak.get`` — returns the page text on 200, the
    bare status int otherwise so the async caller can retry vs.
    escalate. ``timeout`` is forwarded verbatim."""
    import httpcloak

    kwargs: dict[str, str] = {}
    if proxy:
        kwargs["proxy"] = proxy
    r = httpcloak.get(url, timeout=timeout, **kwargs)
    if r.status_code != 200:
        return int(r.status_code)
    content = r.content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def _extract_initial_props(html_text: str) -> dict[str, Any]:
    """Pull ``window.__INITIAL_PROPS__ = JSON.parse("…");`` out of the
    InfoJobs Spain SPA hydration script.

    The payload is JSON-as-string-as-JSON (double-encoded): the outer
    layer is a JS string literal, the inner layer is the actual JSON
    document. We walk the string literal by tracking escapes so the
    embedded ``\\"`` inside JSON keys doesn't terminate the match
    early.

    Returns the decoded dict. Raises :class:`ScraperError` when the
    marker is missing (Distil bounced us to a captcha page) or the
    payload doesn't parse.
    """
    idx = html_text.find(_INITIAL_PROPS_MARKER)
    if idx == -1:
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ not found, the page is "
            "captcha-gated."
        )
    rest = html_text[idx:]
    start = rest.find(_JSON_PARSE_OPEN)
    if start == -1:
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ marker found but "
            "JSON.parse(...) wrapper is missing — site shape changed."
        )
    # Walk forward to the opening quote of the string literal.
    j = start + len(_JSON_PARSE_OPEN)
    while j < len(rest) and rest[j].isspace():
        j += 1
    if j >= len(rest) or rest[j] not in {'"', "'"}:
        raise ScraperError(
            "InfoJobs Spain: JSON.parse argument is not a string "
            "literal — site shape changed."
        )
    quote = rest[j]
    # Walk to the matching closing quote, honoring backslash escapes.
    i = j + 1
    while i < len(rest):
        c = rest[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            break
        i += 1
    if i >= len(rest):
        raise ScraperError(
            "InfoJobs Spain: unterminated JSON.parse string literal."
        )
    quoted = rest[j:i + 1]
    try:
        inner = _decode_js_string_literal(quoted)
        data = json.loads(inner)
    except json.JSONDecodeError as exc:
        raise ScraperError(
            f"InfoJobs Spain: __INITIAL_PROPS__ failed to parse: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ did not decode to an object."
        )
    return data



def _decode_js_string_literal(literal: str) -> str:
    quote = literal[0]
    if len(literal) < 2 or literal[-1] != quote or quote not in {"'", '"'}:
        raise json.JSONDecodeError("not a quoted string", literal, 0)
    out: list[str] = []
    i = 1
    end = len(literal) - 1
    escapes = {
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
        "b": "\\b",
        "f": "\\f",
        "n": "\\n",
        "r": "\\r",
        "t": "\\t",
    }
    while i < end:
        c = literal[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= end:
            raise json.JSONDecodeError("unterminated escape", literal, i)
        esc = literal[i]
        if esc == "u":
            hex_digits = literal[i + 1:i + 5]
            if len(hex_digits) != 4 or not all(
                ch in "0123456789abcdefABCDEF" for ch in hex_digits
            ):
                raise json.JSONDecodeError("invalid unicode escape", literal, i)
            out.append(chr(int(hex_digits, 16)))
            i += 5
            continue
        out.append(escapes.get(esc, esc))
        i += 1
    return "".join(out)

def _page_url(listing_url: str, page: int) -> str:
    parts = urlsplit(listing_url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "page"
    ]
    query.append(("page", str(page)))
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment,
    ))


def _absolutize_link(link: str) -> str:
    """InfoJobs Spain offer links ship in three shapes:

    - ``//www.infojobs.net/path/of-iXXX?args``  (protocol-relative)
    - ``/path/of-iXXX?args``                     (root-relative)
    - ``https://www.infojobs.net/path/...``      (absolute)

    Return a fully-qualified ``https://`` URL in all three cases.
    """
    link = link.strip()
    if link.startswith("//"):
        return f"https:{link}"
    if link.startswith("/"):
        return f"{API_ROOT}{link}"
    if link.startswith("http://") or link.startswith("https://"):
        return link
    # Defensive — unknown shape, prefix the host so we still produce a
    # valid HttpUrl. Tested shapes above cover everything observed.
    return f"{API_ROOT}/{link.lstrip('/')}"


def _infer_remote(teleworking: object) -> bool | None:
    """``teleworking`` is a structured label, not free text. The site
    only ships a known set of values; absence means "not stated"
    which we surface as ``None`` (don't lie to consumers)."""
    if not isinstance(teleworking, str) or not teleworking:
        return None
    if teleworking in _REMOTE_TELEWORKING:
        return True
    if teleworking in _ONSITE_TELEWORKING:
        return False
    return None


def _parse_published_at(raw: object) -> datetime | None:
    """``publishedAt`` ships as ISO-8601 with a ``Z`` suffix, e.g.
    ``2026-05-12T10:41:35Z``. Python's ``fromisoformat`` only handles
    the ``Z`` natively from 3.11+; we normalize manually for safety."""
    if not isinstance(raw, str) or not raw:
        return None
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    except ValueError:
        return None


_SALARY_PERIOD_MAP = {
    "MONTH": "MONTH",
    "YEAR": "YEAR",
    "HOUR": "HOUR",
    "WEEK": "WEEK",
    "DAY": "DAY",
}


def _parse_salary(
    raw: object,
) -> tuple[float | None, float | None, str | None, str | None, str | None]:
    """InfoJobs Spain salary payload shape (when present):

        {"range": {"min": 1200, "max": 1500},
         "period": "MONTH",
         "currency": "EUR",
         "type": "GROSS"}

    Often absent (the field is omitted when the employer didn't
    specify a range). Returns ``(min, max, currency, period, summary)``
    where ``summary`` is a human-readable single-line form for the
    public schema.
    """
    if not isinstance(raw, dict):
        return None, None, None, None, None
    rng = raw.get("range") if isinstance(raw.get("range"), dict) else None
    if not rng:
        return None, None, None, None, None
    smin = rng.get("min")
    smax = rng.get("max")
    smin = float(smin) if isinstance(smin, (int, float)) and smin > 0 else None
    smax = float(smax) if isinstance(smax, (int, float)) and smax > 0 else None
    if smin is None and smax is None:
        return None, None, None, None, None
    currency = raw.get("currency")
    currency = currency if isinstance(currency, str) and len(currency) == 3 else None
    period_raw = raw.get("period")
    period = (
        _SALARY_PERIOD_MAP.get(period_raw)
        if isinstance(period_raw, str) else None
    )
    # Human summary mirrors what the site renders: "1.200 € - 1.500 €
    # / mes". Keep it minimal and locale-agnostic so downstream
    # consumers don't have to parse a Spanish date string.
    parts: list[str] = []
    if smin is not None and smax is not None:
        parts.append(f"{_fmt_amount(smin)} - {_fmt_amount(smax)}")
    elif smin is not None:
        parts.append(f"desde {_fmt_amount(smin)}")
    elif smax is not None:
        parts.append(f"hasta {_fmt_amount(smax)}")
    if currency == "EUR":
        parts.append("€")
    elif currency:
        parts.append(currency)
    if period == "MONTH":
        parts.append("/ mes")
    elif period == "YEAR":
        parts.append("/ año")
    elif period == "HOUR":
        parts.append("/ hora")
    elif period == "WEEK":
        parts.append("/ semana")
    elif period == "DAY":
        parts.append("/ día")
    summary = " ".join(parts) if parts else None
    return smin, smax, currency, period, summary


_AMOUNT_FMT_RE = re.compile(r"(\d)(?=(\d{3})+(?!\d))")


def _fmt_amount(value: float) -> str:
    """Format an integer-like salary as ``1.500`` (Spanish thousands
    separator). Pass-through fractional values as-is — they're rare on
    InfoJobs Spain and the bare repr is fine."""
    if value == int(value):
        s = str(int(value))
        return _AMOUNT_FMT_RE.sub(r"\1.", s)
    return f"{value:.2f}"
