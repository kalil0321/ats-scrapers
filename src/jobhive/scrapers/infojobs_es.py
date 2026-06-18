"""InfoJobs Spain (https://www.infojobs.net) — Spain's #1 job board scraper.

InfoJobs Spain (``infojobs.net``) is the largest direct-posting jobs
board in Spain — ~66,000 live postings as of 2026-05 with ~22 cards
per listing page (so ~3,000 paginated pages cover the whole catalogue).
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

The listing host is gated by Distil + Geetest captcha — bare ``httpx``
hits a 405/captcha. We go straight through ``httpcloak`` (TLS+h2
fingerprint impersonator already shipped in the ``scrapers`` extra and
used by Avature/JazzHR/Eightfold/Built In/Pracuj). Same pattern as
those: optional dependency, falls back gracefully with an empty list +
warning when the user installs the bare package.

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
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.infojobs.net"
LISTING_URL = f"{API_ROOT}/ofertas-trabajo"
DEFAULT_MAX_PAGES = 3500  # ~66k offers / 22 per page = ~3000 pages
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

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
      ~66k-offer catalogue with headroom).
    - ``listing_url`` — override the base URL when restricting to a
      city / category. ``?page=N`` is appended by the scraper; don't
      include it in the override.
    """

    ats = ATSType.INFOJOBSES

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        listing_url: str = LISTING_URL,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max(1, max_pages)
        self.listing_url = listing_url

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        # httpcloak is the only viable transport — bare httpx is gated
        # by Distil + Geetest. Surface a clear install hint when the
        # optional extra is missing rather than crashing mid-fetch.
        from importlib.util import find_spec

        if find_spec("httpcloak") is None:
            log.warning(
                "InfoJobs Spain: httpcloak is required to bypass Distil "
                "captcha. Install with `pip install jobhive[scrapers]`. "
                "Skipping (returning [])."
            )
            return []

        seen: set[str] = set()
        jobs: list[Job] = []
        total_elements: int | None = None
        consecutive_empty = 0
        page = 1
        while page <= self.max_pages and consecutive_empty < 3:
            try:
                payload = await self._fetch_page(page)
            except ScraperError as exc:
                # Deep pagination commonly hits a rate-limit wall once
                # we're past page 200+. Keep what we collected so far
                # rather than throwing it all away; page 1 stays fatal.
                if page == 1:
                    raise
                log.warning(
                    "InfoJobs Spain: stopping pagination at page %d (%s); "
                    "keeping %d jobs collected so far.",
                    page, exc, len(jobs),
                )
                break
            offers = payload.get("offers") or []
            if total_elements is None:
                overview = payload.get("overview") or {}
                te = overview.get("totalElements")
                if isinstance(te, int) and te >= 0:
                    total_elements = te
            new_count = 0
            for offer in offers:
                job = self._parse_offer(offer)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)
                new_count += 1
            if not offers or new_count == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            # Early-exit once we've collected the entire reported
            # catalogue. ``totalElements`` is a server-side count so a
            # small mismatch (new postings published mid-scrape) is
            # fine — the consecutive_empty loop handles the tail.
            if total_elements is not None and len(jobs) >= total_elements:
                break
            page += 1
        return jobs

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
        for attempt in range(1, MAX_RETRIES + 1):
            result = await asyncio.to_thread(_httpcloak_get_sync, url, self.timeout)
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
            commitment=contract_type_raw if isinstance(contract_type_raw, str) else None,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language="es",
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _httpcloak_get_sync(url: str, timeout: float) -> str | int:
    """Sync ``httpcloak.get`` — returns the page text on 200, the
    bare status int otherwise so the async caller can retry vs.
    escalate. ``timeout`` is forwarded verbatim."""
    import httpcloak

    r = httpcloak.get(url, timeout=timeout)
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
            "InfoJobs Spain: __INITIAL_PROPS__ not found — likely "
            "captcha-gated. Verify httpcloak is up to date."
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
