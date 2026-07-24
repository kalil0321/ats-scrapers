"""elempleo.com — Colombia's largest direct-posting job board.

elempleo serves Colombia primarily (10k+ live postings across every
sector — tech is a small slice, the bulk is operations, healthcare,
sales, manufacturing). Companies post directly through the site,
which makes coverage high-signal for the CO market that LinkedIn
under-indexes.

The listing pages at ``https://www.elempleo.com/co/ofertas-empleo?Page=N``
are server-rendered HTML. Each card carries enough fields that we
don't need to fetch detail pages:

  - title — ``<a class="… js-offer-title …">``
  - company — ``<span class="… js-offer-company …">``
  - city — ``<span class="… js-offer-city …">``
  - salary — first ``<div class="text-blue-petrol-dark">`` above
    ``<div class="small-text … ">Salario</div>``
  - contract type, modality (Presencial / Híbrido / Remoto)  —
    same labelled-pair pattern
  - posted date — ``<span class="… js-offer-date …">`` shows relative
    labels such as ``Hoy``, ``Ayer``, or ``Hace 2 días``; unresolved
    ``{{publishDateInfo}}`` template placeholders are ignored
  - id — trailing number in the detail URL slug
    (``/co/ofertas-trabajo/some-title-1886709556`` → ``1886709556``).
    The same id is also embedded as ``data-offer-id`` on the share
    button, which is what we anchor the parser on for robustness.

Pagination uses ``?Page=N`` (capital P matters); pages past the live
tail return HTTP 200 with zero job cards rather than 404, so the
scraper terminates after two consecutive empty pages.

Single-source scraper: ``company_slug`` is ignored — the scraper
sweeps the entire site.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.elempleo.com"
DEFAULT_MAX_PAGES = 1500  # 20 jobs/page × 1500 = 30,000 max — covers
                          # the entire live inventory (~21.7k) with headroom.
                          # Pagination still stops early on the empty-page tail.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0
_BOGOTA_TZ = ZoneInfo("America/Bogota")

# Each result-item is a top-level wrapper card. The class includes a
# trailing space in the live HTML — match flexibly. We use the
# data-offer-id present on the share button inside the card as the
# canonical id; that's the same numeric trailing chunk in the slug
# URL but extracting it from a dedicated attribute is more robust
# than re-parsing the URL.
_CARD_RE = re.compile(
    r'<div class="col-md-12 result-item mb-3 bg-white\s*"(?P<body>.*?)'
    r'(?=<div class="col-md-12 result-item mb-3 bg-white\s*"|<div class="text-center pt-3"|<footer)',
    re.DOTALL,
)
_EMPTY_PAGE_MARKERS = (
    'class="no-results"',
    "class='no-results'",
    'class="text-center pt-3"',
    "class='text-center pt-3'",
)
_OFFER_ID_RE = re.compile(r'data-offer-id="(?P<id>\d+)"')
_DETAIL_HREF_RE = re.compile(
    r'href="(?P<href>/co/ofertas-trabajo/[a-z0-9-]+-(?P<id>\d+))"'
)
_TITLE_RE = re.compile(
    r'class="[^"]*js-offer-title[^"]*"[^>]*title="(?P<t>[^"]+)"'
)
_COMPANY_RE = re.compile(
    r'class="[^"]*js-offer-company[^"]*"[^>]*>\s*(?P<v>[^<]+?)\s*</span>'
)
_CITY_RE = re.compile(
    r'class="[^"]*js-offer-city[^"]*"[^>]*>\s*(?P<v>[^<]+?)\s*</span>'
)
# Labelled-pair pattern: a <div class="text-blue-petrol-dark"> with
# the value, followed by a <div class="small-text …">LABEL</div>.
_LABELLED_VALUE_RE = re.compile(
    r'<div class="text-blue-petrol-dark">\s*(?P<v>[^<]+?)\s*</div>\s*'
    r'<div class="small-text[^"]*">\s*(?P<label>[^<{]+?)\s*</div>',
    re.DOTALL,
)
# js-offer-date wraps the publish hint in a <span> with an icon
# in front; capture whatever text follows the </i>. ``Hoy`` = today.
# Anything else is a Mustache placeholder rendered by JS (``{{…}}``)
# which we ignore.
_DATE_RE = re.compile(
    r'class="[^"]*js-offer-date[^"]*"[^>]*>\s*'
    r'(?:<i[^>]*></i>\s*)?(?P<v>[^<]+?)\s*</span>',
    re.DOTALL,
)
_RELATIVE_DATE_RE = re.compile(r"^hace\s+(?P<days>\d+)\s+dias?$")
# Description fallback: the share modal embeds the full posting body
# as data-offer-description="…". HTML-escaped, multi-line.
_DESCRIPTION_RE = re.compile(
    r'data-offer-description="(?P<v>[^"]*)"', re.DOTALL,
)

# elempleo contract-type → canonical ``EmploymentType`` enum.
#
# Colombian labour-contract terminology:
#   - "Indefinido"             — open-ended permanent contract → FULL_TIME
#   - "Definido" / "Termino fijo" — fixed-term contract → TEMPORARY
#   - "Por obra o labor"       — project / task-based → CONTRACT
#   - "Prestacion de Servicios"— independent contractor → CONTRACT
#   - "Contrato de aprendizaje"— apprenticeship → INTERN
#   - "Practicas"              — internship → INTERN
#   - "Temporal"               — temp agency → TEMPORARY
_EMPLOYMENT_MAP: dict[str, str | None] = {
    "indefinido": "FULL_TIME",
    "termino indefinido": "FULL_TIME",
    "definido": "TEMPORARY",
    "termino fijo": "TEMPORARY",
    "termino definido": "TEMPORARY",
    "temporal": "TEMPORARY",
    "por obra o labor": "CONTRACT",
    "prestacion de servicios": "CONTRACT",
    "freelance": "CONTRACT",
    "contrato de aprendizaje": "INTERN",
    "practicas": "INTERN",
    "otro": None,  # surfaced via commitment, no canonical mapping
}

# Modality (work arrangement) → is_remote signal.
_MODALITY_REMOTE: dict[str, bool] = {
    "remoto": True,
    "presencial": False,
    "hibrido": False,
    "híbrido": False,
}


@ScraperRegistry.register(ATSType.ELEMPLEO)
class ElempleoScraper(BaseScraper):
    """elempleo.com — Colombian national job board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper paginates the entire board.

    Knobs:
    - ``max_pages`` — optional pagination cap for bounded probes. A
      production fetch defaults to a fail-closed 1500-page safety limit.
    """

    ats = ATSType.ELEMPLEO

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self._full_catalogue = max_pages is None
        self.max_pages = min(max_pages or DEFAULT_MAX_PAGES, DEFAULT_MAX_PAGES)

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    async def _fetch_async(self) -> list[Job]:
        seen_ids: set[str] = set()
        jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=self.proxy,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            # Pages out of range return HTTP 200 with zero job cards
            # (rather than 404). Walk until we see two consecutive
            # empty pages — a single empty page in the middle would
            # otherwise be a brittle stop signal if the site ever
            # ships a transient render glitch.
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 2:
                page_jobs = await self._fetch_page(client, sem, page)
                new_count = 0
                for job in page_jobs:
                    if job.ats_id in seen_ids:
                        continue
                    seen_ids.add(job.ats_id or "")
                    jobs.append(job)
                    new_count += 1
                if new_count == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                page += 1
        if self._full_catalogue and not jobs:
            raise ScraperError(
                "elempleo full-catalogue scrape returned no parseable jobs"
            )
        if self._full_catalogue and consecutive_empty < 2:
            raise ScraperError(
                f"elempleo reached its {DEFAULT_MAX_PAGES}-page safety limit "
                "before detecting the end of results"
            )
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> list[Job]:
        url = f"{API_ROOT}/co/ofertas-empleo?Page={page}"
        text = await self._request_html(client, sem, url)
        jobs = list(self._parse_listing(text))
        card_count = sum(1 for _ in _CARD_RE.finditer(text))
        if len(jobs) != card_count:
            raise ScraperError(
                f"elempleo page={page} contained {card_count} job cards "
                f"but parsed {len(jobs)}"
            )
        if card_count == 0 and not any(
            marker in text for marker in _EMPTY_PAGE_MARKERS
        ):
            raise ScraperError(
                f"elempleo page={page} lacked jobs and end-of-results markup"
            )
        return jobs

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/120.0.0.0 Safari/537.36"
                            ),
                            "Accept": "text/html,*/*",
                            "Accept-Language": "es-CO,es;q=0.9,en;q=0.5",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"elempleo fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"elempleo returned {response.status_code} for "
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
                f"elempleo returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"elempleo exhausted retries for {url}: {last_exc}"
        )

    def _parse_listing(self, text: str):
        for match in _CARD_RE.finditer(text):
            body = match.group("body")
            job = self._parse_card(body)
            if job is not None:
                yield job

    def _parse_card(self, body: str) -> Job | None:
        # Prefer the explicit data-offer-id (anchored to the share
        # button); fall back to the trailing-id slug of the detail
        # URL if the share button isn't rendered for some variant.
        id_match = _OFFER_ID_RE.search(body)
        href_match = _DETAIL_HREF_RE.search(body)
        if not href_match:
            return None
        ats_id = id_match.group("id") if id_match else href_match.group("id")
        if not ats_id:
            return None

        title_match = _TITLE_RE.search(body)
        if not title_match:
            return None
        title = html.unescape(title_match.group("t")).strip()
        if not title:
            return None

        company_match = _COMPANY_RE.search(body)
        company = (
            html.unescape(company_match.group("v")).strip()
            if company_match else "Confidencial"
        )

        # Labelled fields — walk the (value, label) pairs once.
        salary_raw: str | None = None
        contract_raw: str | None = None
        modality_raw: str | None = None
        for pair in _LABELLED_VALUE_RE.finditer(body):
            value = html.unescape(pair.group("v")).strip()
            label = pair.group("label").strip().lower()
            if value.startswith("{{") or not value:
                continue
            if label.startswith("salario") and salary_raw is None:
                salary_raw = value
            elif label.startswith("tipo de contrato") and contract_raw is None:
                contract_raw = value
            elif label.startswith("modalidad laboral") and modality_raw is None:
                modality_raw = value

        # City — js-offer-city span; some postings (remote, multi-city)
        # leave it blank. Fall back to ``Colombia`` only when truly
        # absent so country_iso stays accurate.
        city_match = _CITY_RE.search(body)
        city = (
            html.unescape(city_match.group("v")).strip()
            if city_match else ""
        )
        location = (
            f"{city}, Colombia" if city else "Colombia"
        )

        # Modality drives is_remote — listing exposes this directly
        # rather than via the JSON-LD ``jobLocationType`` flag.
        is_remote: bool | None = None
        if modality_raw:
            is_remote = _MODALITY_REMOTE.get(
                _strip_accents(modality_raw).lower()
            )

        # Contract type → normalized employment_type + verbatim commitment.
        employment_type: str | None = None
        commitment: str | None = None
        if contract_raw:
            commitment = contract_raw
            key = _strip_accents(contract_raw).lower()
            employment_type = _EMPLOYMENT_MAP.get(key)

        # Salary — ``$X a $Y millones`` → COP/month range; confidential
        # and unparseable placeholders remain available only in raw.
        salary_min, salary_max, salary_currency, salary_period = (
            _parse_salary(salary_raw)
        )

        # Posted date — listing uses relative Colombian labels. Ignore
        # unresolved Mustache placeholders rather than inventing a date.
        posted_at = None
        date_match = _DATE_RE.search(body)
        if date_match:
            label = html.unescape(date_match.group("v")).strip()
            posted_at = _parse_relative_date(label)

        # Description — pulled from the share modal's data attribute
        # so listings already carry the body. Keep this defensive:
        # the attribute is HTML-escaped and may contain entities.
        description = None
        desc_match = _DESCRIPTION_RE.search(body)
        if desc_match:
            description = _normalize_description(desc_match.group("v"))

        raw: dict[str, Any] = {}
        if salary_raw:
            raw["salary_text"] = salary_raw
        if modality_raw:
            raw["modality"] = modality_raw
        if contract_raw:
            raw["contract_type"] = contract_raw

        return Job(
            url=f"{API_ROOT}{href_match.group('href')}",
            title=title,
            company=company,
            ats_type=ATSType.ELEMPLEO,
            ats_id=ats_id,
            location=location,
            country_iso="CO",
            region="South America",
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period=salary_period,
            salary_summary=salary_raw if salary_currency else None,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,
            commitment=commitment,
            description=description if self.include_descriptions else None,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language="es",
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


_ACCENT_RE = re.compile(r"[áéíóúñÁÉÍÓÚÑ]")
_ACCENT_MAP = str.maketrans(
    "áéíóúñÁÉÍÓÚÑ",
    "aeiounAEIOUN",
)


def _strip_accents(text: str) -> str:
    """ASCII-fold for case-insensitive lookups against the
    ``_EMPLOYMENT_MAP`` / ``_MODALITY_REMOTE`` keys (which are stored
    accent-free for stability — the live HTML may render ``Híbrido``
    with or without the accent depending on encoding)."""
    if not text:
        return ""
    return text.translate(_ACCENT_MAP)


_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_description(raw: str) -> str | None:
    """Decode HTML entities, drop residual tags, collapse whitespace,
    truncate to the schema's ~10kB budget."""
    if not raw:
        return None
    decoded = html.unescape(raw)
    cleaned = _TAG_RE.sub(" ", decoded)
    cleaned = re.sub(r"[\r\n]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned[:10_000] or None


def _parse_relative_date(
    label: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Parse elempleo relative labels at midnight in Colombia."""
    normalized = _strip_accents(label).strip().lower()
    if normalized == "hoy":
        days_ago = 0
    elif normalized == "ayer":
        days_ago = 1
    else:
        match = _RELATIVE_DATE_RE.fullmatch(normalized)
        if not match:
            return None
        days_ago = int(match.group("days"))
    local_now = (now or datetime.now(tz=_BOGOTA_TZ)).astimezone(_BOGOTA_TZ)
    return (
        local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=days_ago)
    ).astimezone(UTC)


# ``$1,5 a $2 millones`` — Colombian salary shorthand. Comma is the
# decimal separator (``1,5`` = 1.5), and ``millones`` is the implicit
# ×1,000,000 COP multiplier. Listings only ever display ranges (or
# the literal ``Salario confidencial``); pre-defined buckets cap at
# ``$15 millones`` and floor at ``$1 millón``.
_SALARY_RANGE_RE = re.compile(
    r"\$\s*(?P<min>[\d.,]+)\s*a\s*\$\s*(?P<max>[\d.,]+)\s*"
    r"(?P<unit>mill[oó]n(?:es)?|mil)",
    re.IGNORECASE,
)
_SALARY_SINGLE_RE = re.compile(
    r"\$\s*(?P<v>[\d.,]+)\s*(?P<unit>mill[oó]n(?:es)?|mil)",
    re.IGNORECASE,
)


def _parse_salary(
    raw: str | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """elempleo salary strings → ``(min, max, currency, period)``.

    - ``$1,5 a $2 millones`` → ``(1_500_000, 2_000_000, "COP", "MONTH")``
    - ``$10 millones``        → ``(10_000_000, 10_000_000, "COP", "MONTH")``
      (single-value, rare on listings)
    - ``Salario confidencial`` → ``(None, None, None, None)``
    - ``""`` / ``None``        → ``(None, None, None, None)``
    """
    if not raw:
        return None, None, None, None
    text = raw.strip()
    range_match = _SALARY_RANGE_RE.search(text)
    if range_match:
        unit = range_match.group("unit").lower()
        multiplier = 1_000_000 if _strip_accents(unit).startswith("millon") else 1_000
        lo = _parse_co_number(range_match.group("min"))
        hi = _parse_co_number(range_match.group("max"))
        if lo is None or hi is None:
            return None, None, None, None
        return lo * multiplier, hi * multiplier, "COP", "MONTH"
    single_match = _SALARY_SINGLE_RE.search(text)
    if single_match:
        unit = single_match.group("unit").lower()
        multiplier = 1_000_000 if _strip_accents(unit).startswith("millon") else 1_000
        value = _parse_co_number(single_match.group("v"))
        if value is None:
            return None, None, None, None
        amount = value * multiplier
        return amount, amount, "COP", "MONTH"
    return None, None, None, None


def _parse_co_number(raw: str) -> float | None:
    """``1,5`` → 1.5; ``12,5`` → 12.5; ``2`` → 2.0.

    elempleo uses the European convention (comma decimal, dot
    thousand) — but the listing buckets never have thousands so this
    is mostly a comma-swap.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    # Defensive: if both . and , are present, dots are thousands.
    if "." in cleaned and "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None
