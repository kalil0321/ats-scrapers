"""NoFluffJobs (https://nofluffjobs.com) — Europe-focused IT/tech jobboard.

NoFluffJobs is a direct-posting tech-jobs board with deep coverage in
Poland and adjacent Central/Eastern European markets (Czech, Germany,
Netherlands), all in English. Listings are tech-heavy with structured
metadata: salary range + currency + period, seniority levels,
technology stack, must-have / nice-to-have requirements, and a
``fullyRemote`` flag — no LinkedIn / Indeed syndication noise.

Public JSON at ``POST https://nofluffjobs.com/api/search/posting``.
The endpoint requires two query parameters, ``salaryCurrency`` and
``salaryPeriod``, which control the *display currency* the response
converts ranges into — it does not narrow the result set. The request
body holds an (often-empty) ``criteriaSearch`` filter object.

Pagination is via ``page`` (1-indexed) + ``pageSize``; ``totalPages``
in the response is the loop bound. The platform caps individual
responses at ~136 entries regardless of requested ``pageSize``.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / remoteok / manfred pattern).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://nofluffjobs.com/api/search/posting"
JOB_URL_TEMPLATE = "https://nofluffjobs.com/job/{slug}"
DEFAULT_CURRENCY = "PLN"
DEFAULT_PERIOD = "month"
DEFAULT_PAGE_SIZE = 200
MAX_PAGES_HARD_CAP = 500
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# NoFluffJobs returns country as ISO 3166-1 alpha-3 (``POL``, ``DEU``,
# ``CZE``…); our Job schema wants alpha-2. Map the common European
# codes that show up on the board; anything else falls back to
# ``None`` so the LLM enrichment pass can take a crack at the
# free-form location string.
_ALPHA3_TO_ALPHA2: dict[str, str] = {
    "POL": "PL", "DEU": "DE", "NLD": "NL", "CZE": "CZ", "SVK": "SK",
    "AUT": "AT", "CHE": "CH", "GBR": "GB", "IRL": "IE", "FRA": "FR",
    "ESP": "ES", "PRT": "PT", "ITA": "IT", "BEL": "BE", "LUX": "LU",
    "DNK": "DK", "SWE": "SE", "NOR": "NO", "FIN": "FI", "EST": "EE",
    "LVA": "LV", "LTU": "LT", "HUN": "HU", "ROU": "RO", "BGR": "BG",
    "HRV": "HR", "SVN": "SI", "GRC": "GR", "UKR": "UA", "BLR": "BY",
    "USA": "US", "CAN": "CA", "ISR": "IL", "ARE": "AE", "TUR": "TR",
    "IND": "IN", "AUS": "AU", "NZL": "NZ", "JPN": "JP", "SGP": "SG",
}

# NoFluffJobs' ``salary.type`` is the contract flavor. Map to the
# normalized ``employment_type`` enum the schema documents:
#   - ``permanent`` (Polish UoP — employee contract) → FULL_TIME
#   - ``b2b`` (Polish B2B — invoiced contractor) → CONTRACT
#   - ``mandate`` / ``zlecenie`` (Polish UZ — civil contract) → CONTRACT
#   - ``freelance`` → CONTRACT
#   - ``internship`` / ``trainee`` → INTERN
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "permanent": "FULL_TIME",
    "b2b": "CONTRACT",
    "mandate": "CONTRACT",
    "zlecenie": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "trainee": "INTERN",
}

# NoFluffJobs' ``salary.period`` is lower-case ('month', 'hour',
# 'year'); our schema's ``SalaryPeriod`` literal is upper-case.
_SALARY_PERIOD_MAP: dict[str, str] = {
    "hour": "HOUR",
    "day": "DAY",
    "week": "WEEK",
    "month": "MONTH",
    "year": "YEAR",
}


@ScraperRegistry.register(ATSType.NOFLUFFJOBS)
class NoFluffJobsScraper(BaseScraper):
    """NoFluffJobs (nofluffjobs.com) — Europe-focused IT job board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``salary_currency`` — display currency the API converts ranges
      into. Must be one of the API's supported codes (``PLN``,
      ``EUR``, ``USD``, ``GBP``, ``CHF``). Does not filter results;
      affects only the numeric range surfaced on ``salary_min`` /
      ``salary_max``. Default ``PLN`` matches the board's native
      currency for the bulk of the listings.
    - ``salary_period`` — display period (``month`` / ``year`` /
      ``hour``). Same display-only semantics as ``salary_currency``.
    - ``page_size`` — pagination batch size. The server caps at ~136
      per response so larger values are quietly clamped; a generous
      default keeps the retry budget low.
    """

    ats = ATSType.NOFLUFFJOBS

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 60.0,
        salary_currency: str = DEFAULT_CURRENCY,
        salary_period: str = DEFAULT_PERIOD,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.salary_currency = salary_currency.upper()
        self.salary_period = salary_period.lower()
        self.page_size = max(1, int(page_size))

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            page = 1
            total_pages = 1
            while page <= total_pages and page <= MAX_PAGES_HARD_CAP:
                payload = await self._fetch_page(client, page)
                postings = payload.get("postings") or []
                if not isinstance(postings, list):
                    raise ScraperError(
                        "NoFluffJobs API shape changed — 'postings' is "
                        f"{type(postings).__name__}, expected list"
                    )
                for item in postings:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                total_pages = _to_int(payload.get("totalPages")) or 1
                # If the server returns no postings on a page that
                # should exist, bail rather than spin — guards against
                # off-by-one server bugs / changes to pagination.
                if not postings:
                    break
                page += 1
        return jobs

    async def _fetch_page(
        self, client: httpx.AsyncClient, page: int
    ) -> dict[str, Any]:
        params = {
            "salaryCurrency": self.salary_currency,
            "salaryPeriod": self.salary_period,
            "page": str(page),
            "pageSize": str(self.page_size),
        }
        body = {"criteriaSearch": {}}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(
                    API_URL,
                    params=params,
                    json=body,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"NoFluffJobs fetch failed: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"NoFluffJobs returned non-JSON: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ScraperError(
                        "NoFluffJobs API shape changed — expected an "
                        f"object, got {type(payload).__name__}"
                    )
                return payload
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"NoFluffJobs returned {response.status_code} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"NoFluffJobs returned {response.status_code}"
            )
        raise ScraperError(f"NoFluffJobs exhausted retries: {last_exc}")

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = (item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        # ``name`` on a NoFluffJobs posting is the *company display name*
        # (their schema is confusingly named: ``title`` = job title,
        # ``name`` = company). Slug-based ``url`` is the routable form.
        company = (item.get("name") or "").strip() or "Unknown"
        slug = (item.get("url") or "").strip()
        if not ats_id or not title or not slug:
            return None

        location_info = item.get("location") or {}
        if not isinstance(location_info, dict):
            location_info = {}
        location, country_iso = _parse_location(location_info)
        is_remote = bool(
            location_info.get("fullyRemote") or item.get("fullyRemote")
        )

        salary = item.get("salary") or {}
        if not isinstance(salary, dict):
            salary = {}
        salary_min = _to_pos_float(salary.get("from"))
        salary_max = _to_pos_float(salary.get("to"))
        salary_currency = (
            salary.get("currency") if (salary_min or salary_max) else None
        )
        salary_period = _SALARY_PERIOD_MAP.get(
            (salary.get("period") or self.salary_period).lower()
        ) if (salary_min or salary_max) else None
        contract_type = (salary.get("type") or "").lower()
        employment_type = _EMPLOYMENT_TYPE_MAP.get(contract_type)

        posted_at = _epoch_ms_to_dt(item.get("posted"))

        raw: dict[str, Any] = {}
        if item.get("category"):
            raw["category"] = item["category"]
        seniority = item.get("seniority")
        if isinstance(seniority, list) and seniority:
            raw["seniority"] = [s for s in seniority if isinstance(s, str)]
        elif isinstance(seniority, str) and seniority:
            raw["seniority"] = [seniority]
        if item.get("technology"):
            raw["technology"] = item["technology"]
        # ``tiles.values`` holds the prominent stack labels surfaced on
        # the card — surface them under ``must_haves`` for consistency
        # with the public spec (the field name maps cleanly onto the
        # detail-page concept).
        tiles = (item.get("tiles") or {}).get("values") if isinstance(
            item.get("tiles"), dict
        ) else None
        if isinstance(tiles, list):
            requirements = [
                t.get("value")
                for t in tiles
                if isinstance(t, dict)
                and t.get("type") == "requirement"
                and isinstance(t.get("value"), str)
            ]
            if requirements:
                raw["must_haves"] = requirements[:20]
        regions = item.get("regions")
        if isinstance(regions, list) and regions:
            raw["regions"] = [r for r in regions if isinstance(r, str)]
        if contract_type:
            raw["contract_type"] = contract_type
        reference = item.get("reference")
        if isinstance(reference, str) and reference:
            raw["reference"] = reference

        return Job(
            url=JOB_URL_TEMPLATE.format(slug=slug),
            title=title,
            company=company,
            ats_type=ATSType.NOFLUFFJOBS,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            language="en",  # NoFluffJobs is English-primary.
            is_remote=is_remote or None,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=contract_type or None,
            requisition_id=reference if isinstance(reference, str) else None,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _parse_location(
    info: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Render NoFluffJobs' multi-place location to a single string plus
    a single ISO 3166-1 alpha-2 country code.

    The API exposes a ``places`` list — each entry is one of (a) a city
    with structured ``country`` object, (b) a freeform city like
    ``"Remote"`` with no country, or (c) a country-only entry. Multiple
    places get pipe-joined; the country code is taken from the first
    place that exposes one. We don't try to derive a country from
    "Remote" labels — that's enrichment territory.
    """
    places = info.get("places")
    if not isinstance(places, list) or not places:
        return None, None
    labels: list[str] = []
    country_iso: str | None = None
    for pl in places:
        if not isinstance(pl, dict):
            continue
        city = (pl.get("city") or "").strip()
        country = pl.get("country") or {}
        country_name = ""
        if isinstance(country, dict):
            code3 = (country.get("code") or "").upper()
            if not country_iso and code3 in _ALPHA3_TO_ALPHA2:
                country_iso = _ALPHA3_TO_ALPHA2[code3]
            country_name = (country.get("name") or "").strip()
        if city and country_name and city.lower() != country_name.lower():
            labels.append(f"{city}, {country_name}")
        elif city:
            labels.append(city)
        elif country_name:
            labels.append(country_name)
    # Dedupe preserving order — multi-place postings frequently repeat
    # the same city under multiple regional flavors.
    seen: set[str] = set()
    unique: list[str] = []
    for lbl in labels:
        if lbl not in seen:
            seen.add(lbl)
            unique.append(lbl)
    location = " | ".join(unique[:5]) if unique else None
    return location, country_iso


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _to_pos_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _epoch_ms_to_dt(value: object) -> datetime | None:
    """NoFluffJobs' ``posted`` is unix-milliseconds (e.g.
    ``1777521835788``). Convert to a naive UTC datetime so the
    schema's ``datetime | None`` field accepts it across consumers
    that compare against naive values."""
    if isinstance(value, bool):
        return None
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).replace(tzinfo=None)
