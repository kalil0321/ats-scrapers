"""InfoJobs Brasil (https://www.infojobs.com.br) — Brazilian job board scraper.

InfoJobs Brasil is one of the largest general-purpose job boards in
Brazil (60k+ active postings at any given time). Companies post
directly to InfoJobs; the site is not aggregated from LinkedIn or
Indeed. The listing pages are ASP.NET server-rendered HTML with
stable ``data-id`` attributes on each card. Detail URLs are slugged
with the numeric id in a trailing ``__{id}.aspx`` suffix:

    /vaga-de-promotor-vendas-em-sao-paulo__11608342.aspx

Pagination uses an AJAX JSON endpoint behind an infinite-scroll
mechanic — the browser hits

    /mf-publicarea/VacancyList/GetVacancyListFragment?url={encoded}

with ``{encoded}`` being a URL-encoded listing URL that carries a
``page=N`` query parameter. The response is JSON with an ``eof``
boolean and a ``listFragmentHTML`` string containing the same card
markup as the SSR page. We use this endpoint exclusively (rather
than the SSR variant) because:

  - The SSR page is heavy (~245kB, full chrome each page) and the
    fragment is ~95kB (just the cards).
  - The SSR page geo-defaults to São Paulo when called from a
    non-Brazilian IP, while the fragment URL we pass is honored
    verbatim.

Each card carries enough fields that we don't need per-job detail
fetches:

  - data-id="11608342"            → ats_id
  - data-href="/vaga-de-X__N.aspx" → url
  - <h2 class="...js_vacancyTitle"> → title
  - <a href="https://.../company"> → company
  - "São Paulo - SP" (free text)    → location
  - js_date data-value="YYYY/MM/DD HH:MM:SS" → posted_at
  - icon-money + "R$ X,XX a R$ Y,YY" → salary
  - icon-buildings / icon-house-and-building → modality
  - text-medium block at bottom → description teaser

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.infojobs.com.br"
FRAGMENT_PATH = "/mf-publicarea/VacancyList/GetVacancyListFragment"
# Default listing URL — country-wide, no filters. The fragment endpoint
# honors the ``page=N`` query string verbatim, unlike the SSR page which
# geolocates the request.
DEFAULT_LISTING_URL = f"{API_ROOT}/vagas-de-emprego.aspx"
DEFAULT_MAX_PAGES = 200  # ~20 cards/page → ~4,000 most-recent jobs
# InfoJobs is friendly but we keep concurrency low to be a polite citizen.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0

# Card delimiter — every result card opens with this exact attribute set.
_CARD_START_RE = re.compile(
    r'<div data-typesimilar="" class="card[^"]*">\s*'
    r'<div id="vacancy(?P<id>\d+)"[^>]*data-id="(?P=id)"[^>]*'
    r'data-href="(?P<href>/vaga[^"]+)"',
)
# Card body extractors (run scoped per card).
_TITLE_RE = re.compile(
    r'<h2[^>]*js_vacancyTitle[^>]*>\s*(?P<t>.*?)\s*</h2>', re.DOTALL,
)
_DATE_RE = re.compile(r'class="js_date" data-value="(?P<v>[^"]+)"')
_COMPANY_LINK_RE = re.compile(
    r'<a class="text-body[^"]*"\s+href="(?P<href>https://www\.infojobs\.com\.br/[^"]+)"[^>]*>'
    r'(?P<name>.*?)</a>',
    re.DOTALL,
)
_LOCATION_RE = re.compile(
    r'<div class="mb-8">\s*(?P<loc>[^<]+?)\s*(?:<|$)',
    re.DOTALL,
)
# Per-icon metadata blocks: <svg class="icon icon-NAME ..."><use .../></svg> TEXT
_ICON_BLOCK_RE = re.compile(
    r'icon icon-(?P<icon>[a-z-]+)\s[^"]*"\s*>\s*<use[^/]+/>\s*</svg>'
    r'\s*(?P<v>[^<]*)',
)
# Description teaser — the last <div class="text-medium"> in the card.
_DESCRIPTION_RE = re.compile(
    r'<div class="text-medium">\s*(?P<v>.*?)\s*</div>', re.DOTALL,
)

# Modality (working method) → ``is_remote`` + raw label.
_MODALITY_LABELS = {
    "buildings": "Presencial",
    "house-and-building": "Híbrido",
    "house": "Home Office",
}

# Brazilian employment-type label → canonical EmploymentType enum. These
# labels appear on detail pages and occasionally in the card teaser; we
# keep the map module-level so future detail-page scraping can reuse it.
_EMPLOYMENT_MAP: dict[str, str] = {
    "efetivo": "FULL_TIME",
    "clt": "FULL_TIME",
    "temporário": "TEMPORARY",
    "temporario": "TEMPORARY",
    "estagiário": "INTERN",
    "estagiario": "INTERN",
    "estágio": "INTERN",
    "estagio": "INTERN",
    "trainee": "FULL_TIME",
    "aprendiz": "INTERN",
    "freelancer": "CONTRACT",
    "freelance": "CONTRACT",
    "autônomo": "CONTRACT",
    "autonomo": "CONTRACT",
    "pj": "CONTRACT",
    "cooperado": "CONTRACT",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BRL_AMOUNT_RE = re.compile(r"R\$\s*([\d.,]+)")


@ScraperRegistry.register(ATSType.INFOJOBSBR)
class InfoJobsBrasilScraper(BaseScraper):
    """InfoJobs Brasil (infojobs.com.br) — Brazilian general-purpose jobs.

    Single-source: ``company_slug`` is ignored. Pass anything (``"any"``,
    ``""``, ``"brasil"``) — the scraper paginates the entire jobs board.

    Knobs:

    - ``max_pages`` — pagination cap (default 200 → ~4,000 jobs).
    - ``listing_url`` — override the base listing URL when you want to
      restrict to a city / category. The page=N parameter is appended
      by the scraper; don't include it in the override.
    """

    ats = ATSType.INFOJOBSBR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        listing_url: str = DEFAULT_LISTING_URL,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self.listing_url = listing_url

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    async def _fetch_async(self) -> list[Job]:
        seen_ids: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            page = 1
            consecutive_empty = 0
            while page <= self.max_pages and consecutive_empty < 3:
                payload = await self._fetch_page(client, sem, page)
                fragment = payload.get("listFragmentHTML") or ""
                eof = bool(payload.get("eof"))
                new_count = 0
                for job in self._parse_listing(fragment):
                    if job.ats_id in seen_ids:
                        continue
                    seen_ids.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1
                if eof:
                    break
                if new_count == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                page += 1
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> dict[str, Any]:
        # Append/overwrite ``page=N`` on the base listing URL. The
        # backend reads the param from the URL we pass in, not from the
        # request URL itself.
        listing = _set_query_param(self.listing_url, "page", str(page))
        encoded = urllib.parse.quote(listing, safe="")
        url = f"{API_ROOT}{FRAGMENT_PATH}?url={encoded}"
        return await self._request_json(client, sem, url)

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": (
                                "Mozilla/5.0 (X11; Linux x86_64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36"
                            ),
                            "Accept": "application/json, text/html;q=0.9",
                            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"InfoJobs Brasil fetch failed for {url}: {exc}"
                        ) from exc
                    response = None
            if response is None:
                # Transport error — back off outside the semaphore so we
                # don't hold a concurrency slot while sleeping.
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"InfoJobs Brasil returned non-JSON for {url}: {exc}"
                    ) from exc
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"InfoJobs Brasil returned {response.status_code} for "
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
                f"InfoJobs Brasil returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"InfoJobs Brasil exhausted retries for {url}: {last_exc}"
        )

    def _parse_listing(self, fragment: str):
        # Split per card on the start marker — every card is bounded by
        # the next ``<div data-typesimilar=""`` or end of fragment.
        starts = list(_CARD_START_RE.finditer(fragment))
        for i, m in enumerate(starts):
            start = m.start()
            end = starts[i + 1].start() if i + 1 < len(starts) else len(fragment)
            body = fragment[start:end]
            job = self._parse_card(
                ats_id=m.group("id"), href=m.group("href"), body=body,
            )
            if job is not None:
                yield job

    def _parse_card(self, *, ats_id: str, href: str, body: str) -> Job | None:
        title_match = _TITLE_RE.search(body)
        if not title_match:
            return None
        title = _strip_html(title_match.group("t"))
        if not title:
            return None

        # Company: linked employer name, else "Empresa confidencial".
        company = "Empresa confidencial"
        link_match = _COMPANY_LINK_RE.search(body)
        if link_match:
            cand = _strip_html(link_match.group("name"))
            # Strip trailing "verified" tooltip text the link sometimes
            # wraps inline (e.g. "SODEXO Este selo indica…"). The badge
            # tooltip leaks through entity decoding; keep only the first
            # line / cap to a sane length.
            cand = cand.split("Este selo indica")[0].strip()
            if cand:
                company = cand

        # Location lives in <div class="mb-8">Cidade - UF<…> when present.
        # "Home office" sometimes leaks in here too — keep it; downstream
        # ``_infer_remote`` reads the value to decide ``is_remote``.
        location = None
        loc_match = _LOCATION_RE.search(body)
        if loc_match:
            raw_loc = _strip_html(loc_match.group("loc"))
            if raw_loc:
                location = raw_loc

        # Metadata icons: money / suitcase / graduate-hat / buildings /
        # house-and-building. We only key on the ones that map to schema
        # fields; the rest go into ``raw``.
        icon_values: dict[str, str] = {}
        for m in _ICON_BLOCK_RE.finditer(body):
            v = _strip_html(m.group("v"))
            if v and m.group("icon") not in icon_values:
                icon_values[m.group("icon")] = v

        salary_raw = icon_values.get("money")
        salary_min, salary_max, salary_currency, salary_summary = _parse_salary(
            salary_raw
        )

        # Modality / remote inference. icon-house-and-building =
        # Híbrido, icon-buildings = Presencial. "Remoto" / "Home
        # office" can appear on either the buildings text or the
        # location string.
        modality_raw = None
        for icon, label in _MODALITY_LABELS.items():
            if icon in icon_values:
                modality_raw = icon_values[icon] or label
                break
        is_remote = _infer_remote(modality_raw, location)

        # Employment type: rarely surfaced on the card (lives on
        # the detail page) but mapped defensively when it is.
        commitment_raw = None
        for k in ("file", "file-alt", "document", "suitcase"):
            if k in icon_values and any(
                t in icon_values[k].lower() for t in _EMPLOYMENT_MAP
            ):
                commitment_raw = icon_values[k]
                break
        employment_type = (
            _match_employment_type(commitment_raw) if commitment_raw else None
        )

        # Description teaser — the last text-medium block in the card.
        # Multiple may match; take the longest, which is always the
        # description rather than the date/rating snippets.
        description = None
        desc_candidates = [
            _strip_html(m.group("v"))
            for m in _DESCRIPTION_RE.finditer(body)
        ]
        desc_candidates = [d for d in desc_candidates if d]
        if desc_candidates:
            description = max(desc_candidates, key=len)

        # posted_at: structured datetime in the hidden js_date block.
        posted_at = None
        date_match = _DATE_RE.search(body)
        if date_match:
            posted_at = _parse_brazilian_date(date_match.group("v"))

        raw: dict[str, Any] = {}
        if "graduate-hat" in icon_values:
            raw["education"] = icon_values["graduate-hat"]
        if "suitcase" in icon_values:
            raw["experience_label"] = icon_values["suitcase"]
        if modality_raw:
            raw["modality"] = modality_raw
        if salary_raw and salary_raw != salary_summary:
            raw["salary_raw"] = salary_raw

        url = href if href.startswith("http") else f"{API_ROOT}{href}"

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.INFOJOBSBR,
            ats_id=ats_id,
            location=location,
            country_iso="BR",
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period="MONTH" if salary_currency else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment_raw,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language="pt",
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return _WS_RE.sub(" ", cleaned).strip()


def _match_employment_type(raw: str) -> str | None:
    """Map a (possibly composite) Brazilian employment label to a
    canonical ``EmploymentType``. Labels are frequently composite
    (e.g. ``"Efetivo CLT"``, ``"Estágio / Trainee"``) so we match the
    map keys as tokens/substrings rather than requiring an exact hit.
    The first matching key (in map insertion order) wins."""
    lowered = raw.lower()
    for key, value in _EMPLOYMENT_MAP.items():
        if key in lowered:
            return value
    return None


def _set_query_param(url: str, key: str, value: str) -> str:
    """Return ``url`` with ``key=value`` set in the query string,
    overwriting any existing value. Preserves order of other params."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params = [(k, v) for k, v in params if k != key]
    params.append((key, value))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))


def _parse_brl_amount(raw: str) -> float | None:
    """``R$ 3.000`` → 3000.0; ``R$ 3.500,50`` → 3500.50.

    Brazilian currency uses ``.`` as the thousand separator and ``,``
    as the decimal point, opposite to en-US conventions.
    """
    if not raw:
        return None
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_salary(
    raw: str | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """InfoJobs salary strings:

    - ``"A combinar"``                       → no signal
    - ``"R$ 1.700,00 a R$ 2.000,00"``        → min=1700, max=2000, BRL
    - ``"Até R$ 5.000,00"``                  → max only
    - ``"A partir de R$ 8.000,00"``          → min only
    - empty / None                            → no signal

    Returns ``(min, max, currency, summary)``. ``summary`` is the
    original string with whitespace normalized so the public schema
    has a verbatim form of what users see; it's ``None`` when no
    numeric amount was extractable (e.g. "A combinar").
    """
    if not raw:
        return None, None, None, None
    normalized = _WS_RE.sub(" ", raw).strip()
    nums = _BRL_AMOUNT_RE.findall(normalized)
    if not nums:
        return None, None, None, None
    parsed = [_parse_brl_amount(n) for n in nums]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return None, None, None, None
    lower = normalized.lower()
    if len(parsed) == 1:
        if "até" in lower or "ate" in lower:
            return None, parsed[0], "BRL", normalized
        if "partir" in lower or "a partir" in lower:
            return parsed[0], None, "BRL", normalized
        return parsed[0], parsed[0], "BRL", normalized
    return parsed[0], parsed[-1], "BRL", normalized


def _infer_remote(modality_raw: str | None, location: str | None) -> bool | None:
    """Combine the modality label and location text to infer remote.

    Returns ``True`` when either signal explicitly states remote; the
    canonical ``is_remote`` field accepts both ``True`` and ``False``
    here because the modality icon is structured (i.e. absence really
    is evidence of on-site, unlike the title-only heuristic).
    """
    sources = [s.lower() for s in (modality_raw, location) if s]
    if not sources:
        return None
    blob = " ".join(sources)
    if "home office" in blob or "remoto" in blob or "remote" in blob:
        return True
    if "híbrido" in blob or "hibrido" in blob or "hybrid" in blob:
        return False
    if "presencial" in blob or "on-site" in blob:
        return False
    return None


def _parse_brazilian_date(raw: str) -> datetime | None:
    """``js_date`` carries an exact ``YYYY/MM/DD HH:MM:SS`` value —
    the human-readable "Hoje" / "Ontem" / "Há 3 dias" labels are
    derived from this so we just parse the structured form."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(
                tzinfo=ZoneInfo("America/Sao_Paulo"),
            ).astimezone(UTC)
        except ValueError:
            continue
    return None
