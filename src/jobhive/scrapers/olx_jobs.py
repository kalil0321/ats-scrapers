"""OLX Jobs (multi-country classifieds) — Eastern Europe + Asia jobs scraper.

OLX runs country-specific marketplaces (``olx.pl``, ``olx.ua``, ``olx.bg``,
``olx.ro``, ``olx.pt``, ``olx.kz``, ``olx.uz``, …) where job postings live
under a top-level ``praca`` / ``rabota`` / ``locuri-de-munca`` /
``anuncios-de-emprego`` category. Each property exposes the same v1 offers
REST API at ``https://www.olx.{tld}/api/v1/offers``; the only per-country
delta is the numeric ``category_id`` rooting the praca / rabota tree
(``4`` on PL/RO, ``6`` on UA/KZ/UZ, ``190`` on PT, ``606`` on BG).

The API caps each query at ``offset ≤ 1000`` and returns a cursor via
``links.next`` that goes null once exhausted — we walk that cursor per
country, dedupe by region-scoped id, and stop. To scrape beyond the
1000-cap one would need to slice by region/city; out of scope for the
initial scraper (each top-level cap already returns the freshest 1000
roles per country, which is the useful slice for an ATS dataset).

Single-source / multi-tenant: ``company_slug`` selects *which country*
to scrape. Pass one of the supported region codes (``pl``, ``ua``, ``ro``,
``bg``, ``pt``, ``kz``, ``uz``) or ``"all"`` to enumerate the full set.
Mirrors the ``WantedScraper`` / ``BundesagenturScraper`` pattern.

Countries probed but **not** supported:

* ``za`` — ``olx.co.za`` is shut down (serves a static "no longer
  available" HTML page on every endpoint).
* ``eg`` — Egypt sits on the rebranded ``dubizzle.com.eg`` and is
  protected by a JS fingerprint challenge, not the OLX v1 API. Add a
  dedicated Dubizzle scraper separately if the volume is worth it.

Both are documented as TODOs so the next pass can pick them up without
re-doing the probing.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlparse

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any


PER_PAGE = 40  # Default page size the JSON UI uses; bigger limits 400.
OFFSET_CAP = 1000  # API rejects offset > 1000 with HTTP 400.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

_TAG_RE = re.compile(r"<[^>]+>")


class _Region:
    """Per-country configuration for an OLX property.

    Captured as a plain class (not a dataclass) so attribute access is
    cheap in the hot loop and IDE autocomplete works inside the scraper.
    """

    __slots__ = (
        "category_id",
        "code",
        "continent",
        "country_iso",
        "currency",
        "language",
        "tld",
    )

    def __init__(
        self,
        code: str,
        tld: str,
        category_id: int,
        country_iso: str,
        language: str,
        currency: str,
        continent: str,
    ) -> None:
        self.code = code
        self.tld = tld
        self.category_id = category_id
        self.country_iso = country_iso
        self.language = language
        self.currency = currency
        self.continent = continent

    @property
    def host(self) -> str:
        return f"https://www.olx.{self.tld}"

    @property
    def api_url(self) -> str:
        return f"{self.host}/api/v1/offers"


# Country → API config. Verified live against each property's
# ``/api/v1/offers?category_id=<id>`` endpoint. The ``language`` /
# ``currency`` columns are the listing locale, not the user's; they're
# what the API returns inside the payload.
_REGIONS: dict[str, _Region] = {
    "pl": _Region("pl", "pl",    4, "PL", "pl", "PLN", "Europe"),
    "ua": _Region("ua", "ua",    6, "UA", "uk", "UAH", "Europe"),
    "ro": _Region("ro", "ro",    4, "RO", "ro", "RON", "Europe"),
    "bg": _Region("bg", "bg",  606, "BG", "bg", "BGN", "Europe"),
    "pt": _Region("pt", "pt",  190, "PT", "pt", "EUR", "Europe"),
    "kz": _Region("kz", "kz",    6, "KZ", "ru", "KZT", "Asia"),
    "uz": _Region("uz", "uz",    6, "UZ", "uz", "UZS", "Asia"),
    # TODO(olx): za (olx.co.za is decommissioned), eg (dubizzle.com.eg
    # is fingerprint-protected). Both probed during initial scoping.
}


@ScraperRegistry.register(ATSType.OLX_JOBS)
class OlxJobsScraper(BaseScraper):
    """OLX classifieds → jobs across PL/UA/RO/BG/PT/KZ/UZ.

    Pass a region code as ``company_slug`` to pick the country:

        OlxJobsScraper("pl").fetch()  # Poland (~1k freshest roles)
        OlxJobsScraper("all").fetch() # All supported countries

    Each country's API caps at ``offset=1000`` so a single call returns
    at most ~1k jobs per region (the freshest slice). Beyond that
    requires slicing by city/region, which is out of scope here.
    """

    ats = ATSType.OLX_JOBS

    SUPPORTED_REGIONS: ClassVar[tuple[str, ...]] = tuple(_REGIONS)

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.regions = _resolve_regions(company_slug)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]], region: _Region) -> None:
            async with lock:
                for it in items:
                    job = _parse_job(it, region=region)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_region(region: _Region) -> None:
                # Start at offset=0 and walk ``links.next`` until null or
                # we hit the 1000-cap (the API returns next=null at the
                # cap, so the natural cursor-walk terminates correctly).
                url: str | None = (
                    f"{region.api_url}?category_id={region.category_id}"
                    f"&offset=0&limit={PER_PAGE}"
                )
                while url:
                    payload = await self._request_json(client, sem, url)
                    items = payload.get("data") or []
                    if not items:
                        return
                    await absorb(items, region)
                    next_url = (payload.get("links") or {}).get("next")
                    if isinstance(next_url, dict):
                        # The /api/v1/offers endpoint surfaces ``next``
                        # as ``{"href": "..."}``; the wanted/eures path
                        # has it as a bare string. Handle both shapes.
                        next_url = next_url.get("href")
                    if not next_url or not isinstance(next_url, str):
                        return
                    url = next_url

            await asyncio.gather(*(per_region(r) for r in self.regions))
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with sem:
                    response = await client.get(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"OLX fetch failed for {url}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"OLX returned non-JSON for {url}: {exc}"
                    ) from exc
            if response.status_code == 400:
                if _is_offset_cap_url(url):
                    return {"data": [], "links": {}}
                raise ScraperError(f"OLX returned 400 for {url}")
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"OLX returned {response.status_code} for "
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
                f"OLX returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"OLX exhausted retries for {url}: {last_exc}"
        )


# --- parsing ----------------------------------------------------------------

# OLX wraps its param values in a uniform shape. The ones we care about:
#
#   { "key": "salary", "value": {"from": 8000, "to": 16000,
#     "type": "monthly", "currency": "PLN", "gross": True, ... }}
#   { "key": "type",   "value": {"key": "fulltime", "label": "Pełny etat"}}
#   { "key": "workplace", "value": {"key": ["remote"], "label": ...}}
#
# Map raw OLX employment-type tokens to the canonical schema's
# ``EmploymentType`` literal. Anything unrecognised stays None and the
# raw label rides in ``commitment`` so the LLM enrichment can pick it up.
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "fulltime": "FULL_TIME",
    "full_time": "FULL_TIME",
    "parttime": "PART_TIME",
    "part_time": "PART_TIME",
    "additional": "PART_TIME",
    "contract": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "temporary": "TEMPORARY",
    "seasonal": "TEMPORARY",
}

# OLX exposes salary as ``type`` ∈ {hourly, daily, weekly, monthly, yearly}.
_SALARY_PERIOD_MAP: dict[str, str] = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "yearly": "YEAR",
    "annually": "YEAR",
}


def _is_offset_cap_url(url: str) -> bool:
    """OLX returns 400 after the documented offset cap; only that
    pagination sentinel should be treated as end-of-data."""
    values = parse_qs(urlparse(url).query).get("offset") or []
    if not values:
        return False
    try:
        return int(values[0]) > OFFSET_CAP
    except ValueError:
        return False


def _resolve_regions(company_slug: str) -> tuple[_Region, ...]:
    """Pick the region tuple for a given ``company_slug``.

    Accepts a single region code (``"pl"``), ``"all"`` for every
    supported country, or a comma-separated list (``"pl,ua,ro"``).
    Unknown codes raise — fail loudly so a typoed slug doesn't silently
    no-op.
    """
    slug = (company_slug or "").strip().lower()
    if not slug or slug in {"all", "any", "*"}:
        return tuple(_REGIONS.values())
    parts = [p.strip() for p in slug.split(",") if p.strip()]
    out: list[_Region] = []
    for code in parts:
        region = _REGIONS.get(code)
        if region is None:
            raise ScraperError(
                f"OlxJobsScraper: unknown region {code!r}. "
                f"Known: {sorted(_REGIONS)} (or 'all')."
            )
        out.append(region)
    return tuple(out)


def _parse_job(item: dict[str, Any], *, region: _Region) -> Job | None:
    raw_id = item.get("id")
    if raw_id is None:
        return None
    # OLX ids are per-country, so prefix with the region code to avoid
    # cross-country collisions (id 12345 exists on both olx.pl and olx.ua).
    ats_id = f"{region.code}:{raw_id}"
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not ats_id or not title or not url:
        return None

    params = {
        p.get("key"): p
        for p in (item.get("params") or [])
        if isinstance(p, dict) and p.get("key")
    }

    # Employer name: OLX has both ``user.name`` (always present, often a
    # first-name for individual posters) and ``user.company_name``
    # (set when the poster registered a business account). Prefer the
    # explicit company name, fall back to the user's display name, then
    # to ``"Unknown"`` so the row still passes Job validation.
    user = item.get("user") or {}
    company_name = (
        (user.get("company_name") or "").strip()
        or (user.get("name") or "").strip()
        or "Unknown"
    )

    location = _format_location(item.get("location") or {})

    lat, lon = _extract_latlon(item.get("map") or {})

    salary_min, salary_max, salary_currency, salary_period, salary_summary = (
        _extract_salary(params, fallback_currency=region.currency)
    )

    employment_type, commitment = _extract_employment(params)
    is_remote = _extract_is_remote(params)
    department = _extract_industry(params)
    experience = _extract_experience(params)

    description = item.get("description")
    cleaned_desc = _strip_html(description) if isinstance(description, str) else None

    posted_at = _parse_iso(item.get("created_time"))

    business = bool(item.get("business"))

    raw: dict[str, Any] = {"region": region.code}
    if business:
        raw["business"] = True
    user_id = user.get("id")
    if user_id is not None:
        raw["user_id"] = user_id
    user_uuid = user.get("uuid")
    if isinstance(user_uuid, str) and user_uuid:
        raw["user_uuid"] = user_uuid
    cat = item.get("category") or {}
    cat_id = cat.get("id")
    if cat_id is not None:
        raw["category_id"] = cat_id
    valid_to = item.get("valid_to_time")
    if isinstance(valid_to, str) and valid_to:
        raw["valid_to_time"] = valid_to
    refresh = item.get("last_refresh_time")
    if isinstance(refresh, str) and refresh:
        raw["last_refresh_time"] = refresh

    return Job(
        url=url,
        title=title,
        company=company_name,
        ats_type=ATSType.OLX_JOBS,
        ats_id=ats_id,
        location=location,
        country_iso=region.country_iso,
        region=region.continent,
        lat=lat,
        lon=lon,
        is_remote=is_remote,
        salary_currency=salary_currency,
        salary_period=salary_period,
        salary_summary=salary_summary,
        salary_min=salary_min,
        salary_max=salary_max,
        experience=experience,
        employment_type=employment_type,
        department=department,
        commitment=commitment,
        description=cleaned_desc,
        posted_at=posted_at,
        fetched_at=datetime.now(tz=UTC),
        language=region.language,
        raw=raw,
    )


def _format_location(loc: dict[str, Any]) -> str | None:
    """OLX ``location`` ships as ``{city: {name}, district: {name},
    region: {name}}``. Format city-first, then district, then region.

    The values are in the listing language (Polish names for PL, Cyrillic
    Ukrainian for UA, …) so we keep them verbatim — translating them
    here would break ``location`` as a deterministic display string.
    """
    parts: list[str] = []
    for key in ("city", "district", "region"):
        sub = loc.get(key)
        if not isinstance(sub, dict):
            continue
        name = sub.get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in parts:
            parts.append(name.strip())
    return ", ".join(parts) if parts else None


def _extract_latlon(map_obj: dict[str, Any]) -> tuple[float | None, float | None]:
    """OLX ``map`` ships lat/lon as numbers when geocoded. ``show_detailed``
    is False on most consumer postings (the API blurs the exact pin) but
    the broad lat/lon is still useful for region-level analytics."""
    lat = map_obj.get("lat")
    lon = map_obj.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None, None
    # Reject the (0, 0) fallback OLX sometimes returns when geocoding
    # failed (a Ukrainian job is never legitimately at null island).
    if lat == 0 and lon == 0:
        return None, None
    return float(lat), float(lon)


def _extract_salary(
    params: dict[str, dict[str, Any]],
    *,
    fallback_currency: str,
) -> tuple[
    float | None, float | None, str | None, str | None, str | None
]:
    """Parse the ``salary`` param. Returns
    ``(min, max, currency, period, summary)``.

    OLX shape:

        {"key": "salary", "value": {"from": 8000, "to": 16000,
         "type": "monthly", "currency": "PLN", "gross": True, ...}}

    When ``from`` and ``to`` are both null the param exists only as a
    visibility flag; we skip those.
    """
    entry = params.get("salary")
    if not entry:
        return None, None, None, None, None
    value = entry.get("value")
    if not isinstance(value, dict):
        return None, None, None, None, None

    min_amt = _to_float(value.get("from"))
    max_amt = _to_float(value.get("to"))
    if min_amt is None and max_amt is None:
        return None, None, None, None, None

    raw_currency = value.get("currency")
    currency = (
        raw_currency.strip().upper()
        if isinstance(raw_currency, str) and len(raw_currency.strip()) == 3
        else fallback_currency
    )
    period = _SALARY_PERIOD_MAP.get(str(value.get("type") or "").lower())

    # Summary is the most useful form when only one bound is known
    # (e.g. "up to 32 PLN/h"). Build it from the structured parts so
    # downstream consumers see a consistent free-text form.
    summary = _format_salary_summary(min_amt, max_amt, currency, period)
    return min_amt, max_amt, currency, period, summary


def _format_salary_summary(
    min_amt: float | None,
    max_amt: float | None,
    currency: str,
    period: str | None,
) -> str:
    if min_amt is not None and max_amt is not None and min_amt != max_amt:
        body = f"{_fmt_num(min_amt)} – {_fmt_num(max_amt)} {currency}"
    elif min_amt is not None and max_amt is not None:
        body = f"{_fmt_num(min_amt)} {currency}"
    elif max_amt is not None:
        body = f"up to {_fmt_num(max_amt)} {currency}"
    else:
        body = f"from {_fmt_num(min_amt or 0)} {currency}"
    if period:
        body += f" / {period.lower()}"
    return body


def _fmt_num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _extract_employment(
    params: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    """OLX's ``type`` param holds employment type as a slug + label:

        {"key": "type",
         "value": {"key": "fulltime", "label": "Pełny etat"}}

    Map the slug to the canonical enum and keep the localised label as
    ``commitment`` so we don't lose granularity (e.g. PL distinguishes
    between full / part / additional shifts that all collapse to the
    same enum value)."""
    entry = params.get("type") or params.get("employment_type")
    if not entry:
        return None, None
    value = entry.get("value")
    if not isinstance(value, dict):
        return None, None
    raw_key = value.get("key")
    raw_label = value.get("label")

    employment_type: str | None = None
    if isinstance(raw_key, str):
        employment_type = _EMPLOYMENT_TYPE_MAP.get(raw_key.lower())
    elif isinstance(raw_key, list):
        # Some properties ship as multi-select (``["fulltime", "parttime"]``).
        # Pick the first match.
        for k in raw_key:
            if isinstance(k, str):
                mapped = _EMPLOYMENT_TYPE_MAP.get(k.lower())
                if mapped:
                    employment_type = mapped
                    break

    commitment = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None
    return employment_type, commitment


def _extract_is_remote(params: dict[str, dict[str, Any]]) -> bool | None:
    """OLX surfaces remote on the ``workplace`` param as a checkboxes
    value: ``{"key": ["remote", "hybrid", "on_site"], ...}``. We assert
    ``True`` only when ``remote`` is explicitly present — mirrors the
    rest of the codebase's "only ever set True, leave False to the
    enrichment pass" rule."""
    entry = params.get("workplace")
    if not entry:
        return None
    value = entry.get("value")
    if not isinstance(value, dict):
        return None
    keys = value.get("key")
    if isinstance(keys, list):
        flat = [k.lower() for k in keys if isinstance(k, str)]
        if any(k in {"remote", "home"} for k in flat):
            return True
    elif isinstance(keys, str):
        if keys.lower() in {"remote", "home"}:
            return True
    return None


def _extract_industry(params: dict[str, dict[str, Any]]) -> str | None:
    """``industry`` is OLX's coarse department-equivalent. Use the
    localised label rather than the slug (``ind_re`` → "Nieruchomości,
    budownictwo") so the column stays human-readable per region."""
    entry = params.get("industry")
    if not entry:
        return None
    value = entry.get("value")
    if not isinstance(value, dict):
        return None
    label = value.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return None


def _extract_experience(params: dict[str, dict[str, Any]]) -> int | None:
    """OLX's experience field is a boolean ("required" vs "not required")
    rather than a year count, so we can't populate ``Job.experience``
    (which is an int year-count). Return None — the enum lives in the
    ``raw`` dict if needed.

    Kept as a function so the call site reads cleanly and a future
    extension (some properties might surface a numeric range) has an
    obvious home."""
    return None


def _to_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_iso(value: object) -> datetime | None:
    """OLX returns ISO-8601 with offset (``2026-04-02T18:09:11+02:00``).
    ``datetime.fromisoformat`` handles this directly on 3.11+."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    """Strip HTML tags + entities, collapse whitespace, truncate to
    the schema's 10kB description budget. OLX descriptions are raw
    user-posted HTML (``<p>``, ``<br>``, ``<ul>``, ``<li>``) so this
    needs to be tolerant of malformed markup."""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:10_000]
