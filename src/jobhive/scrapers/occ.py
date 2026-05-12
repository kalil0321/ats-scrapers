"""OCC Mundial — the dominant job board in Mexico.

OCC Mundial (``occ.com.mx``) is Mexico's largest jobboard. The
listings page (``https://www.occ.com.mx/empleos``) is rendered by a
Next.js app and hydrates ~20 postings per page from an Apollo GraphQL
``ROOT_QUERY → jobsByUrl(...)`` payload embedded in
``<script id="__NEXT_DATA__">``. Every Apollo entry under
``initialApolloState["Job:<id>"]`` carries the full structured posting
— id, title, description, location, salary range, employment type,
work mode, publish date, friendly URL slug — so we don't need to walk
detail pages for the canonical fields.

OCC sits behind **two** independent bot-managers:

  - **Cloudflare** in front of ``www.occ.com.mx`` — serves the classic
    "Just a moment…" interstitial on every page until the JS
    challenge clears. ``curl`` / ``httpx`` / plain ``requests`` get
    a hard 403 with no cookie warmup.
  - **PerimeterX** in front of ``api.occ.com.mx`` — returns a JSON
    ``{"errors":[{"code":"PXYS-2","description":"Unknow API client"}]}``
    on every request without a valid ``_px3`` cookie + sensor data.

Reverse-engineered on 2026-05-12 via ``reverse-api-engineer`` (HAR
capture) + Wayback Machine archived listings. ``api.occ.com.mx`` is
the *real* JSON backend but requires a full browser-emitted
PerimeterX challenge (``/_px/captcha``) — out of scope for a TLS-only
client. The HTML+``__NEXT_DATA__`` route is the same data the page
itself consumes for SSR, so this scraper targets that surface.

Strategy:

1. ``httpcloak`` with a rotating list of TLS-impersonation presets
   (mirrors Bayt / Kariyer). Cloudflare's challenge matrix is
   non-deterministic — some IP×TOD combinations clear with
   ``chrome-latest-windows``, others want ``ios-safari-18``. We try
   each in rank order; the first one that returns a non-challenge
   200 wins and the same session is reused for the entire paginated
   walk (so the bot-manager doesn't re-challenge mid-walk).
2. Each successful page is parsed by extracting the
   ``__NEXT_DATA__`` script body, ``json.loads()``-ing it, and
   walking ``props.pageProps.initialApolloState`` for entries whose
   key starts with ``Job:``. The ``ROOT_QUERY`` ``jobList`` order is
   preserved so we can dedupe sticky/featured results across pages.
3. Graceful degradation: when **every** preset is blocked we log a
   warning and return ``[]``. Same optional-fallback contract as
   Bayt / Kariyer / Tesla — a missed scrape is preferable to a
   crash in the publish pipeline.

URL pattern (canonical, what we store in ``Job.url``):

    https://www.occ.com.mx/empleo/oferta/{friendlyId}/

where ``friendlyId`` is ``{numeric_id}-{slug}``. We strip the
tracking query string OCC appends to the in-Apollo ``url`` field
(``?rank=...&sessionid=...&uuid=...&utm_origin=...``) because those
parameters are per-impression noise — not part of the canonical
identity of the posting.

Single-source scraper: the ``company_slug`` constructor argument
selects a category / region URL segment under ``/empleos/…``.
Examples:

  - ``"all"`` (default) → ``/empleos`` — the unfiltered MX-wide feed.
  - ``"ciudad-de-mexico"`` → ``/empleos/en-ciudad-de-mexico/``
  - ``"jalisco"`` → ``/empleos/en-jalisco/``
  - ``"nuevo-leon"`` → ``/empleos/en-nuevo-leon/``
  - ``"medio-tiempo"`` → ``/empleos/medio-tiempo/`` (part-time)
  - ``"becario-practicas"`` → ``/empleos/becario-practicas/``
    (internship)

Any path under ``/empleos/...`` works — pass the raw slug (without
leading/trailing slashes); we resolve it against the base URL.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

# --- URL constants ----------------------------------------------------

_SITE_BASE = "https://www.occ.com.mx"

# Listing pagination — OCC uses a ``?page=N`` query string. Page 1 is
# the bare URL (no ``page=`` parameter); subsequent pages append it.
_DEFAULT_SLUG = "all"

# Page-size budget. OCC serves 20–22 jobs per listing page in
# practice; the catalogue is ~130k jobs MX-wide so a full walk would
# need ~6500 pages. The default cap stops a runaway loop if OCC
# regresses on the "no more rows" sentinel.
DEFAULT_MAX_PAGES = 500

# Cloudflare retry knobs. The first request of a session frequently
# gets a 403 even with a valid TLS fingerprint — back off briefly and
# rotate presets. After exhausting all presets we surface a warning
# and return ``[]``; mid-pagination failures end the loop and we keep
# whatever pages we already have.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# httpcloak TLS presets to try in rank order. Cloudflare's challenge
# matrix isn't deterministic — some IP×TOD combinations clear with
# ``chrome-latest-windows``, others want ``ios-safari-18``. Each
# preset uses a fresh ``Session`` so we don't carry a tainted cookie
# jar across attempts.
_HTTPCLOAK_PRESETS: tuple[str, ...] = (
    "chrome-latest-windows",
    "chrome-latest-macos",
    "safari-latest",
    "ios-safari-18",
    "firefox-latest",
    "android-chrome-latest",
)

# Headers a real Chromium-on-Windows browser sends. Cloudflare scores
# missing ``Sec-Fetch-*`` highly so we mirror them even though they're
# nominally optional. ``Accept-Language: es-MX`` matches what OCC's
# Spanish locale serves.
_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Cloudflare's challenge page is identifiable by its title — we use
# this to distinguish "real" 200-status content from CF clearing
# pages that occasionally come back as 200 with a JS challenge body.
_CLOUDFLARE_TITLE = "<title>Just a moment..."

# Pin the Next.js hydration script. OCC's app embeds the full
# Apollo state in a single ``<script id="__NEXT_DATA__" type="application/json">``
# tag — this regex is a tolerant extractor that survives whitespace
# / attribute reordering. We capture the raw JSON body and parse with
# ``json.loads`` (faster than soup-walking, and the structure is
# guaranteed JSON by Next.js's SSR contract).
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(?P<body>.+?)</script>',
    re.DOTALL,
)

# OCC's ``Job.url`` field embeds a tracking query string
# (``?rank=...&sessionid=...&uuid=...&utm_origin=...&utm_channel=...&page=...``).
# Strip the whole query so the canonical URL we store is stable
# across impressions. The path itself sometimes already trails a
# ``/`` (``/empleo/oferta/{id}-{slug}/``), sometimes not — we
# normalise to the trailing-slash form because that's what JSON-LD
# / Google for Jobs uses.
_CANONICAL_URL_TEMPLATE = "https://www.occ.com.mx/empleo/oferta/{friendly_id}/"


@ScraperRegistry.register(ATSType.OCC)
class OCCMexicoScraper(BaseScraper):
    """OCC Mundial (Mexico) jobs scraper.

    Constructor knobs:
        company_slug: a path segment under ``/empleos/``. ``"all"``
            (default) hits the unfiltered MX-wide feed at
            ``/empleos``. Any other value is treated as a category /
            region slug (e.g. ``"en-ciudad-de-mexico"``,
            ``"medio-tiempo"``) and resolves to
            ``/empleos/{slug}/``. Pass the slug without leading or
            trailing slashes.
        max_pages: stop after this many pages even if more remain.
            OCC's pagination uses ``?page=N``; ``page=1`` is the
            bare URL.

    The scraper depends on the optional ``httpcloak`` extra. When the
    library isn't installed (``pip install jobhive`` without the
    ``[scrapers]`` extra) it logs a warning and returns ``[]`` —
    same optional-fallback contract as Bayt / Kariyer / Tesla.
    """

    ats = ATSType.OCC

    def __init__(
        self,
        company_slug: str = _DEFAULT_SLUG,
        *,
        timeout: float = 60.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        normalized = (company_slug or _DEFAULT_SLUG).strip().strip("/")
        if not normalized:
            normalized = _DEFAULT_SLUG
        super().__init__(normalized, timeout=timeout)
        self.slug = normalized
        self.max_pages = max_pages

    # ----- public entry point -----------------------------------------

    def fetch(self) -> list[Job]:
        if not _httpcloak_available():
            _warn_httpcloak_disabled()
            return []
        return list(self._fetch_via_httpcloak())

    # ----- URL helpers ------------------------------------------------

    def _build_url(self, page: int) -> str:
        """Compose the listing URL for ``page`` (1-indexed).

        ``page=1`` is the bare path (no ``page=`` query) because that's
        the URL Next.js's SSR populates the initial Apollo state for.
        Subsequent pages append ``?page=N``. The ``all`` sentinel
        resolves to ``/empleos`` (no trailing segment); any other
        slug resolves to ``/empleos/{slug}/``.
        """
        base = _SITE_BASE + "/empleos"
        if self.slug != _DEFAULT_SLUG:
            base = f"{base}/{self.slug}/"
        if page <= 1:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page}"

    # ----- core fetch loop --------------------------------------------

    def _fetch_via_httpcloak(self) -> Iterable[Job]:
        """Paginate the listing pages until OCC stops returning new
        rows. Dedupe by Apollo ``Job:<id>`` because "premium" and
        sticky rows recur across pages.
        """
        fetched_at = datetime.now(tz=UTC)
        seen: set[str] = set()
        jobs: list[Job] = []
        session, first_page_html = self._open_session_and_first_page()
        if session is None or first_page_html is None:
            return jobs
        try:
            for page_no in range(1, self.max_pages + 1):
                page_html: str | None = (
                    first_page_html
                    if page_no == 1
                    else self._fetch_page(session, page_no)
                )
                if page_html is None:
                    break
                page_rows = list(_iter_apollo_jobs(page_html))
                if not page_rows:
                    # End of pagination, or OCC stopped rendering the
                    # ``ROOT_QUERY → jobsByUrl`` hydration we depend on.
                    break
                new_in_page = 0
                for job_id, raw_job in page_rows:
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    job = self._parse_job(raw_job, fetched_at=fetched_at)
                    if job is not None:
                        jobs.append(job)
                        new_in_page += 1
                if new_in_page == 0:
                    # Every row on this page was already seen
                    # (sticky / premium tail) — stop before we burn
                    # budget on repeats.
                    break
        finally:
            with _SuppressedClose():
                session.close()
        log.info(
            "OCC %s: fetched %d unique jobs across up to %d pages",
            self.slug, len(jobs), self.max_pages,
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
            url = self._build_url(1)
            try:
                response = session.get(
                    url, headers=_HEADERS, timeout=self.timeout,
                )
            except httpcloak.HTTPCloakError as exc:
                log.warning(
                    "OCC: preset %s transport error: %s", preset, exc,
                )
                with _SuppressedClose():
                    session.close()
                continue

            last_status = response.status_code
            text = response.text
            if response.status_code == 200 and _CLOUDFLARE_TITLE not in text:
                log.info("OCC: preset %s cleared Cloudflare", preset)
                return session, text
            log.info(
                "OCC: preset %s blocked (status=%d, cf=%s)",
                preset, response.status_code, _CLOUDFLARE_TITLE in text,
            )
            with _SuppressedClose():
                session.close()

        log.warning(
            "OCC: all %d TLS presets blocked by Cloudflare "
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

        url = self._build_url(page_no)
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
                        "OCC %s page=%d transport error after %d retries: %s",
                        self.slug, page_no, MAX_RETRIES, exc,
                    )
                    return None
                _sleep_backoff(attempt)
                continue

            status = response.status_code
            text = response.text
            if status == 200 and _CLOUDFLARE_TITLE not in text:
                return text
            if status in (403, 429) or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    log.warning(
                        "OCC %s page=%d returned %d (cf=%s) after "
                        "%d retries — stopping pagination",
                        self.slug, page_no, status,
                        _CLOUDFLARE_TITLE in text, MAX_RETRIES,
                    )
                    return None
                _sleep_backoff(attempt)
                continue
            log.warning(
                "OCC %s page=%d returned unexpected status %d — stopping",
                self.slug, page_no, status,
            )
            return None

        log.warning(
            "OCC %s page=%d exhausted retries: %s",
            self.slug, page_no, last_exc,
        )
        return None

    # ----- single-row parser ------------------------------------------

    def _parse_job(
        self,
        raw: dict[str, Any],
        *,
        fetched_at: datetime,
    ) -> Job | None:
        """Convert one Apollo ``Job`` entry into a canonical ``Job``.

        Returns ``None`` when the bare minimum is missing (id +
        title + friendlyId / url). OCC occasionally surfaces stub
        entries for expired postings; skipping is preferable to
        fabricating values.
        """
        job_id = _coerce_str(raw.get("id"))
        title = _coerce_str(raw.get("title"))
        if not job_id or not title:
            return None

        friendly_id = _coerce_str(raw.get("friendlyId")) or _friendly_id_from_url(
            _coerce_str(raw.get("url")),
        )
        if not friendly_id:
            # Fall back to the raw id; the canonical URL won't have a
            # slug but it'll still resolve.
            friendly_id = job_id
        url = _CANONICAL_URL_TEMPLATE.format(friendly_id=friendly_id)

        # --- company ---
        company_block = raw.get("company") or {}
        company_name = (
            _coerce_str(company_block.get("namePretty"))
            or _coerce_str(company_block.get("name"))
            or "Empresa confidencial"
        )
        is_confidential = bool(company_block.get("confidential"))

        # --- location ---
        location_block = raw.get("location") or {}
        location_text = _coerce_str(location_block.get("description"))
        locations = location_block.get("locations") or []
        state_code = None
        city_name = None
        if locations and isinstance(locations[0], dict):
            first = locations[0]
            state = first.get("state") or {}
            city = first.get("city") or {}
            state_code = _coerce_str(state.get("abbreviation"))
            city_name = _coerce_str(city.get("jobCity")) or _coerce_str(
                city.get("description"),
            )
        # All OCC postings are in Mexico (the platform is MX-only).
        # The ``country`` ref under each ``locations[*]`` is always
        # ``CountryLocation:MX``; we hardcode the ISO code because
        # cross-referencing the apollo ref every row is wasted
        # cycles for a single-country site.
        country_iso = "MX"
        region = "North America"

        # --- salary ---
        salary = raw.get("salary") or {}
        salary_currency: str | None = None
        salary_min: float | None = None
        salary_max: float | None = None
        salary_summary: str | None = None
        if salary.get("show"):
            try:
                from_amt = float(salary.get("from") or 0)
                to_amt = float(salary.get("to") or 0)
            except (TypeError, ValueError):
                from_amt = to_amt = 0.0
            if from_amt > 0 or to_amt > 0:
                salary_currency = "MXN"
                salary_min = from_amt or None
                salary_max = to_amt or None
                if from_amt and to_amt and from_amt != to_amt:
                    salary_summary = (
                        f"${int(from_amt):,} - ${int(to_amt):,} MXN"
                    )
                elif from_amt:
                    salary_summary = f"${int(from_amt):,} MXN"
                elif to_amt:
                    salary_summary = f"${int(to_amt):,} MXN"

        # --- hiring / employment_type ---
        hiring = raw.get("hiring") or {}
        employment_type = _classify_employment(hiring)
        commitment = _commitment_label(hiring)

        # --- workMode ---
        workmode = raw.get("workMode") or {}
        workmode_desc = _coerce_str(workmode.get("description"))
        is_remote: bool | None = None
        if workmode_desc == "REMOTE":
            is_remote = True
        elif workmode_desc == "HYBRID":
            # Hybrid roles are partially remote — keep the flag
            # ``None`` so downstream LLM enrichment can decide based
            # on the description; the raw mode is preserved in
            # ``raw["work_mode"]``.
            is_remote = None
        elif workmode_desc == "IN_PERSON":
            is_remote = False

        # --- dates ---
        dates = raw.get("dates") or {}
        posted_at = _parse_naive_mx_datetime(_coerce_str(dates.get("publish")))

        # --- description ---
        description = _coerce_str(raw.get("description"))

        # --- raw overflow ---
        raw_overflow: dict[str, object] = {
            "category": _ref_name(raw.get("category")),
            "subcategory": _ref_name(raw.get("subcategory")),
            "job_type": _coerce_str(raw.get("jobType")),
            "work_mode": workmode_desc,
            "state": state_code,
            "city": city_name,
            "is_confidential": is_confidential,
            "profile_id": _coerce_str(raw.get("profileId")),
            "rank": raw.get("rank"),
        }

        return Job(
            url=url,
            title=title,
            company=company_name,
            ats_type=ATSType.OCC,
            ats_id=job_id,
            location=location_text,
            country_iso=country_iso,
            region=region,
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period="MONTH" if salary_currency else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,
            commitment=commitment,
            posted_at=posted_at,
            fetched_at=fetched_at,
            language="es",
            description=description,
            raw=raw_overflow,
        )


# --- module-level helpers ---------------------------------------------


def _httpcloak_available() -> bool:
    """Return True if ``httpcloak`` is importable.

    Mirrors the optional-fallback contract used by Bayt / Kariyer /
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
        "OCC: httpcloak required to bypass Cloudflare bot manager — "
        "install with `pip install jobhive[scrapers]`. Skipping.",
    )


def _iter_apollo_jobs(html_body: str) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(job_id, raw_job_dict)`` for each ``Job:<id>`` in the
    ``__NEXT_DATA__`` hydration blob on a listing page.

    Order is the same as ``ROOT_QUERY → jobsByUrl(...) → jobList``
    when that key is present in the Apollo state — that's the
    user-visible order so it doubles as a stable iteration order
    for dedup. When ``ROOT_QUERY`` is missing (older OCC builds, or
    listing pages with the query keyed on a non-default URL), we
    fall back to dict insertion order on ``initialApolloState``.
    """
    m = _NEXT_DATA_RE.search(html_body)
    if m is None:
        return
    try:
        data = json.loads(m.group("body"))
    except json.JSONDecodeError:
        return
    try:
        apollo = data["props"]["pageProps"]["initialApolloState"]
    except (KeyError, TypeError):
        return
    if not isinstance(apollo, dict):
        return

    # Prefer the explicit jobList order from ROOT_QUERY when present.
    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    root_query = apollo.get("ROOT_QUERY")
    if isinstance(root_query, dict):
        for key, value in root_query.items():
            if not key.startswith("jobsByUrl"):
                continue
            if not isinstance(value, dict):
                continue
            job_list = value.get("jobList")
            if not isinstance(job_list, list):
                continue
            for ref in job_list:
                if not isinstance(ref, dict):
                    continue
                ref_key = ref.get("__ref")
                if not isinstance(ref_key, str) or not ref_key.startswith("Job:"):
                    continue
                job_id = ref_key[len("Job:"):]
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    ordered_ids.append(job_id)

    # Fall back to walking the apollo state directly if ROOT_QUERY
    # didn't surface any references (older builds / unusual URLs).
    if not ordered_ids:
        for key in apollo:
            if not isinstance(key, str) or not key.startswith("Job:"):
                continue
            job_id = key[len("Job:"):]
            if job_id and job_id not in seen_ids:
                seen_ids.add(job_id)
                ordered_ids.append(job_id)

    for job_id in ordered_ids:
        entry = apollo.get(f"Job:{job_id}")
        if isinstance(entry, dict):
            yield job_id, entry


def _coerce_str(value: object) -> str | None:
    """Return a stripped ``str`` for non-empty inputs, otherwise None."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    # OCC occasionally surfaces ints (e.g. ``id`` on some endpoints)
    # — accept and stringify defensively.
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _friendly_id_from_url(url: str | None) -> str | None:
    """Extract the ``{id}-{slug}`` segment from a ``/empleo/oferta/...``
    path. Returns ``None`` when the URL doesn't match the expected
    shape — the caller falls back to the bare numeric id."""
    if not url:
        return None
    parts = urlsplit(url)
    # Strip the query so we can match the path cleanly.
    path = parts.path
    m = re.match(r"^/empleo/oferta/([^/?]+)", path)
    if m:
        return m.group(1)
    return None


def _strip_url_query(url: str) -> str:
    """Drop the query / fragment from a URL but keep scheme + path."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _classify_employment(hiring: dict[str, Any]) -> str | None:
    """Map OCC's boolean hiring flags to the canonical
    ``EmploymentType``.

    OCC sets multiple booleans simultaneously (e.g. ``fullTime`` AND
    ``permanent``). We pick the most specific *commitment*-level
    flag (intern / temporary / contract) first, then fall back to
    full-time / part-time.
    """
    if not isinstance(hiring, dict):
        return None
    if hiring.get("temporary"):
        return "TEMPORARY"
    if hiring.get("contract"):
        return "CONTRACT"
    if hiring.get("partTime"):
        return "PART_TIME"
    if hiring.get("fullTime"):
        return "FULL_TIME"
    return None


def _commitment_label(hiring: dict[str, Any]) -> str | None:
    """Preserve OCC's permanent / temporary distinction even when
    we've normalized ``employment_type`` to FULL_TIME. ``commitment``
    is the free-form bag for ATS-specific labels the canonical
    enum doesn't capture."""
    if not isinstance(hiring, dict):
        return None
    parts: list[str] = []
    if hiring.get("fullTime"):
        parts.append("Tiempo completo")
    if hiring.get("partTime"):
        parts.append("Medio tiempo")
    if hiring.get("contract"):
        parts.append("Por contrato")
    if hiring.get("permanent"):
        parts.append("Permanente")
    if hiring.get("temporary"):
        parts.append("Temporal")
    return " · ".join(parts) if parts else None


def _ref_name(value: object) -> str | None:
    """Apollo entries reference related entities via
    ``{"__ref": "JobCategory:5"}``. We don't have the rest of the
    Apollo state in scope for this helper (the parser is row-local),
    so we surface just the id portion as a coarse classification
    signal. Downstream consumers can join against a category dict if
    they care about the human-readable name."""
    if not isinstance(value, dict):
        return None
    ref = value.get("__ref")
    if not isinstance(ref, str) or ":" not in ref:
        return None
    return ref.split(":", 1)[1] or None


def _parse_naive_mx_datetime(value: str | None) -> datetime | None:
    """Parse OCC's ``"2024-02-21 00:00:00"`` date strings into UTC.

    OCC reports dates in CDMX local time without a timezone suffix.
    We don't have a reliable timezone library here, so we treat the
    timestamp as UTC for the canonical ``posted_at`` — the downstream
    LLM / dedup layer can adjust if the 6-hour offset matters for
    its use case. The error is bounded (≤6h) and the ``raw``
    overflow preserves the original string when it's salient.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


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
    ``contextlib.suppress(Exception)`` but the explicit class shows
    up consistently across our Cloudflare-gated scrapers
    (Bayt / Kariyer / OCC).
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


# Keep this near the bottom so the rest of the module reads top-down.
# ``ScraperError`` is re-exported for tests / callers that want to
# raise from a custom orchestrator without an explicit import.
__all__ = ["OCCMexicoScraper", "ScraperError"]
