"""HeadHunter (hh.ru / hh.kz / hh.ee / hh.uz / hh.kg / hh.by) — the
dominant Russian + CIS job platform.

HeadHunter exposes a free, well-documented REST API at
``api.hh.{ru,kz,ee,uz,kg,by}``. The Russian property alone carries
~1M live postings; the Kazakhstan and Estonian properties add another
~150k + ~30k respectively, with the rest covering smaller CIS markets.

The API is geo-restricted: requests from US/EU datacenter IPs return
``{"errors":[{"type":"forbidden"}]}``. Production runs route through
a residential proxy (Evomi). Pass ``proxy_url`` explicitly or set the
``PROXY`` env var — both the standard ``http://user:pass@host:port``
URL form and the 4-colon ``host:port:user:pass`` shape some providers
ship are accepted (same helper jobs.ch / Programathor / Tesla use).

The "company" in ``company_slug`` selects the country property to hit:
``"ru"``, ``"kz"``, ``"ee"``, ``"uz"``, ``"kg"``, ``"by"``. Each shares
the API surface — what differs is the regional emphasis (and the IP
country the proxy needs to exit through).

API caps ``(page + 1) * per_page`` at 2000 total results per query, so
enumerating the whole ~1M corpus requires slicing the search space.
``slice_by`` controls the slicing strategy:

- ``"area"`` (default) — iterate top-level region codes
  (1 = Moscow, 2 = Saint Petersburg, …). Cheapest fan-out with the
  best coverage for ``ru`` and ``kz``.
- ``"none"`` — single query, capped at 2000 results. Useful for tests
  or for tiny markets (``ee`` returns far below the cap).

Doc reference: https://github.com/hhru/api/blob/master/docs_eng/README.md
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

# --- API constants ----------------------------------------------------------

# Host per country property. ``company_slug`` keys into this dict to
# pick the right one. Kept short — the keys match the canonical lower-
# case country code we expect operators to pass.
HOSTS: dict[str, str] = {
    "ru": "api.hh.ru",
    "kz": "api.hh.kz",
    "ee": "api.hh.ee",
    "uz": "api.hh.uz",
    "kg": "api.headhunter.kg",
    "by": "api.hh.by",
}

# ISO 3166-1 alpha-2 country code per host. Set on every Job so
# downstream consumers can filter by country without parsing
# ``area.name`` (which is Russian-language and city-level).
COUNTRY_ISO: dict[str, str] = {
    "ru": "RU",
    "kz": "KZ",
    "ee": "EE",
    "uz": "UZ",
    "kg": "KG",
    "by": "BY",
}

# Locale of the listings on each property — the postings are written
# in the country's primary language. Pinned per-host because the API
# itself doesn't surface a language tag.
LANGUAGE: dict[str, str] = {
    "ru": "ru",
    "kz": "kk",  # Kazakh — practically many listings are in Russian
    "ee": "et",
    "uz": "uz",
    "kg": "ky",
    "by": "be",
}

# Per-property top-level region codes. The full hh.ru tree is ~1000
# nodes (countries → federal districts → oblasts → cities) but the
# ``area`` filter supports any node — passing a parent fans out to
# children automatically. These are the root regions that yield
# meaningful slicing without exploding fan-out.
#
# References:
# - https://api.hh.ru/areas — full tree
# - The codes are stable and global across hh.* properties.
AREAS: dict[str, list[str]] = {
    # hh.ru — Russia. 113 is "Russia"; sub-regions split by federal
    # district to keep each query under the 2000-result cap. The
    # ones picked here are the densest federal subjects (Moscow + St
    # Petersburg alone account for ~40% of the corpus, then a long
    # tail of regional capitals).
    "ru": [
        "1",     # Moscow
        "2",     # Saint Petersburg
        "3",     # Yekaterinburg (Sverdlovsk Oblast)
        "4",     # Novosibirsk
        "66",    # Nizhny Novgorod
        "68",    # Kazan
        "76",    # Rostov-on-Don
        "78",    # Samara
        "88",    # Krasnodar
        "104",   # Krasnoyarsk
        "1438",  # Volgograd
        "1646",  # Voronezh
        "1652",  # Perm
        "1716",  # Saratov
        "1530",  # Ufa (Bashkortostan)
        "1124",  # Ulyanovsk
        "1202",  # Tver
        "1146",  # Vladimir
        "2114",  # Sochi
        "2019",  # Tyumen
    ],
    "kz": [
        "159",  # Almaty
        "160",  # Astana (Nur-Sultan)
        "161",  # Aktobe
        "162",  # Atyrau
        "164",  # Karaganda
        "165",  # Kyzylorda
        "167",  # Pavlodar
        "172",  # Shymkent
        "174",  # Oskemen
    ],
    "ee": [
        "1486",  # Tallinn
        "1490",  # Tartu
        "1487",  # Narva
    ],
    "uz": [
        "97",   # Tashkent
        "2734", # Samarkand
        "2735", # Bukhara
    ],
    "kg": [
        "2470",  # Bishkek
        "2471",  # Osh
    ],
    "by": [
        "16",  # Minsk
        "17",  # Brest
        "18",  # Vitebsk
        "20",  # Gomel
        "21",  # Grodno
        "22",  # Mogilev
    ],
}

PER_PAGE = 100
# API hard cap: (page + 1) * per_page <= 2000. With per_page=100 this
# leaves us 20 pages of headroom per slice (0..19).
MAX_PAGES_PER_SLICE = 20
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
DEFAULT_TIMEOUT = 30.0

# hh wants a contact email in the UA. The address is read straight off
# the Cargo project — change if the contact owner changes.
USER_AGENT = "jobhive/1.0 (kalil.bouzigues@gmail.com)"

# Map hh ``employment.id`` values onto the canonical EmploymentType
# enum. ``project`` / ``probation`` / ``volunteer`` collapse to
# CONTRACT — none of them are a literal fit, but CONTRACT is the
# closest cross-ATS bucket consumers expect when filtering "not
# salaried full-time".
EMPLOYMENT_MAP: dict[str, str] = {
    "full": "FULL_TIME",
    "part": "PART_TIME",
    "project": "CONTRACT",
    "probation": "CONTRACT",
    "volunteer": "CONTRACT",
}

# Map hh ``experience.id`` to an integer year-count. ``moreThan6`` is
# clamped to 6 — the API doesn't expose the actual upper bound.
EXPERIENCE_MAP: dict[str, int] = {
    "noExperience": 0,
    "between1And3": 1,
    "between3And6": 3,
    "moreThan6": 6,
}

# Currency codes returned by the API → ISO 4217. ``RUR`` is the legacy
# code for the Russian ruble — the canonical schema uses ``RUB``.
# ``BYR`` (legacy Belarusian ruble, redenominated 2016) is mapped to
# ``BYN``. Everything else is passed through as-is when already ISO.
CURRENCY_MAP: dict[str, str] = {
    "RUR": "RUB",
    "RUB": "RUB",
    "USD": "USD",
    "EUR": "EUR",
    "KZT": "KZT",
    "BYR": "BYN",
    "BYN": "BYN",
    "UAH": "UAH",
    "UZS": "UZS",
    "KGS": "KGS",
    "AZN": "AZN",
    "GEL": "GEL",
}


def _resolve_proxy_url(raw: str | None) -> str | None:
    """Accept the 4-colon ``host:port:user:pass`` shape some
    residential-proxy providers ship and convert to the standard
    ``http://user:pass@host:port`` URL httpx expects. Plain
    ``http(s)://…`` URLs pass through.

    Mirrors the helper in ``programathor.py`` and ``jobsch.py``; kept
    local so the scraper has no internal cross-imports.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^http://([^:/@]+):(\d+):([^:]+):(.+)$", raw)
    if m:
        host, port, user, pw = m.groups()
        return f"http://{user}:{pw}@{host}:{port}"
    return raw


@ScraperRegistry.register(ATSType.HH)
class HHRuScraper(BaseScraper):
    """HeadHunter (hh.ru + CIS) scraper.

    ``company_slug`` is the country property to hit — ``"ru"``,
    ``"kz"``, ``"ee"``, ``"uz"``, ``"kg"``, ``"by"``. Each maps to a
    different host but the API surface is identical.

    Knobs:

    - ``proxy_url`` — explicit proxy URL. Falls back to ``PROXY`` env
      var (4-colon shape auto-converted), then to direct connection.
      hh.ru returns 403 to US/EU datacenter IPs — pretty much always
      needed in production from a cloud VM.
    - ``slice_by`` — slicing strategy to fan past the 2000-result API
      cap. ``"area"`` (default) iterates the regional codes in
      :data:`AREAS`; ``"none"`` runs a single unsliced query.
    - ``areas`` — explicit override of the area code list, useful for
      testing or for narrowing the run to a single city.
    - ``max_pages_per_slice`` — pagination ceiling within one slice.
      The API caps at 20 (page 0..19 × per_page 100 = 2000 results).
    """

    ats = ATSType.HH

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        proxy_url: str | None = None,
        slice_by: str = "area",
        areas: list[str] | None = None,
        max_pages_per_slice: int = MAX_PAGES_PER_SLICE,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        slug = (company_slug or "").lower().strip()
        if slug not in HOSTS:
            raise ScraperError(
                f"HHRuScraper: unknown company_slug {company_slug!r}; "
                f"expected one of {sorted(HOSTS)}"
            )
        self.country = slug
        self.host = HOSTS[slug]
        self.country_iso = COUNTRY_ISO[slug]
        self.language = LANGUAGE[slug]
        self.proxy_url = _resolve_proxy_url(proxy_url) or _resolve_proxy_url(
            os.environ.get("PROXY")
        )
        if slice_by not in {"area", "none"}:
            raise ScraperError(
                f"HHRuScraper: unsupported slice_by={slice_by!r}; "
                "expected 'area' or 'none'"
            )
        self.slice_by = slice_by
        self.areas = areas if areas is not None else AREAS.get(slug, [])
        self.max_pages_per_slice = min(max_pages_per_slice, MAX_PAGES_PER_SLICE)

    # --- public entry ------------------------------------------------------

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        slices = self._build_slices()

        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
            # Residential proxies occasionally MITM TLS with a CA chain
            # that isn't in the system trust store; the requests carry
            # no PII so the cost of disabling verify is low.
            client_kwargs["verify"] = False

        async with httpx.AsyncClient(**client_kwargs) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def run_slice(slice_params: dict[str, str]) -> None:
                async for item in self._iter_slice(client, sem, slice_params):
                    job = self._build_job(item)
                    if job is None or job.ats_id is None:
                        continue
                    async with lock:
                        if job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)

            await asyncio.gather(*(run_slice(s) for s in slices))

        return jobs

    # --- slicing -----------------------------------------------------------

    def _build_slices(self) -> list[dict[str, str]]:
        """Cartesian product of the active slicing dimensions.

        Currently a single dimension (area) → list of one-key dicts.
        Kept dict-shaped so future ``slice_by="role"`` / ``"text"``
        modes can slot in without changing the iteration code.
        """
        if self.slice_by == "none" or not self.areas:
            return [{}]
        return [{"area": code} for code in self.areas]

    async def _iter_slice(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slice_params: dict[str, str],
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk pages within a single (area / role / text) slice."""
        page = 0
        while page < self.max_pages_per_slice:
            payload = await self._fetch_page(client, sem, slice_params, page)
            items = payload.get("items") or []
            if not items:
                return
            for item in items:
                yield item
            pages_in_slice = payload.get("pages") or 0
            if page + 1 >= pages_in_slice:
                return
            page += 1

    # --- HTTP --------------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slice_params: dict[str, str],
        page: int,
    ) -> dict[str, Any]:
        url = f"https://{self.host}/vacancies"
        params: dict[str, str | int] = {
            "per_page": PER_PAGE,
            "page": page,
            **slice_params,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url,
                        params=params,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"hh.{self.country} fetch failed for {url} "
                            f"params={params}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    payload: dict[str, Any] = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"hh.{self.country} returned non-JSON 200 for {url} "
                        f"params={params}: {exc}"
                    ) from exc
                return payload
            if response.status_code == 403:
                raise ScraperError(
                    f"hh.{self.country} returned 403 for {url} — the API "
                    "geo-blocks non-CIS IPs; set the PROXY env variable "
                    "(or pass proxy_url) and route through a residential "
                    "exit in Russia/CIS"
                )
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"hh.{self.country} returned {response.status_code} "
                        f"for {url} params={params} after {MAX_RETRIES} retries"
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
                f"hh.{self.country} returned {response.status_code} for {url} "
                f"params={params}"
            )
        raise ScraperError(
            f"hh.{self.country} exhausted retries for {url} params={params}: "
            f"{last_exc}"
        )

    # --- parsing -----------------------------------------------------------

    def _build_job(self, item: dict[str, Any]) -> Job | None:
        ats_id = item.get("id")
        url = item.get("alternate_url")
        title = item.get("name")
        if not ats_id or not url or not title:
            return None

        employer = item.get("employer") or {}
        company = employer.get("name") or "Unknown"

        area = item.get("area") or {}
        location = area.get("name") or None

        salary = item.get("salary") or {}
        currency = salary.get("currency") if isinstance(salary, dict) else None
        salary_currency: str | None = None
        salary_min: float | None = None
        salary_max: float | None = None
        salary_period: str | None = None
        if currency:
            mapped = CURRENCY_MAP.get(currency.upper())
            if mapped:
                salary_currency = mapped
                salary_period = "MONTH"
                salary_min = _as_float(salary.get("from"))
                salary_max = _as_float(salary.get("to"))

        snippet = item.get("snippet") or {}
        description = _join_snippet(
            snippet.get("requirement"), snippet.get("responsibility")
        )

        employment = item.get("employment") or {}
        emp_id = employment.get("id") if isinstance(employment, dict) else None
        employment_type = EMPLOYMENT_MAP.get(emp_id) if emp_id else None
        commitment = (
            employment.get("name")
            if isinstance(employment, dict) and employment.get("name")
            else None
        )

        experience = item.get("experience") or {}
        exp_id = experience.get("id") if isinstance(experience, dict) else None
        experience_years = EXPERIENCE_MAP.get(exp_id) if exp_id else None

        posted_at = _parse_published_at(item.get("published_at"))

        raw: dict[str, Any] = {
            "host": self.host,
        }
        if employer.get("id"):
            raw["employer_id"] = employer["id"]
        if area.get("id"):
            raw["area_id"] = area["id"]
        schedule = item.get("schedule")
        if schedule:
            raw["schedule"] = schedule
        type_ = item.get("type")
        if type_:
            raw["type"] = type_
        professional_roles = item.get("professional_roles")
        if professional_roles:
            raw["professional_roles"] = professional_roles

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.HH,
            ats_id=str(ats_id),
            location=location,
            country_iso=self.country_iso,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment,
            experience=experience_years,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language=self.language,
            raw=raw,
        )


# --- module-level helpers ---------------------------------------------------


def _as_float(value: object) -> float | None:
    """``salary.from`` / ``salary.to`` come back as ints when present
    or ``null`` when not. Defensive ``float()`` since the API has been
    known to ship occasional string values for currency conversions."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _join_snippet(requirement: str | None, responsibility: str | None) -> str | None:
    """hh.ru's ``snippet`` field carries two short HTML-marked-up
    strings — combine into a single plain-text blob.

    Highlight tags (``<highlighttext>``) wrap matched keywords when
    the listing was returned via a text search. Strip them so the
    description doesn't ship with markup.
    """
    parts = [p for p in (requirement, responsibility) if p]
    if not parts:
        return None
    joined = "\n".join(parts)
    cleaned = _HTML_TAG_RE.sub("", joined)
    cleaned = cleaned.replace("&nbsp;", " ").replace("&amp;", "&")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


# hh's ``published_at`` is ISO-8601 with a numeric offset:
# ``2026-05-12T10:30:00+0300``. ``datetime.fromisoformat`` on 3.11+
# accepts the ``+HH:MM`` form; we normalize ``+HHMM`` → ``+HH:MM``
# before parsing.
_TZ_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})$")


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    fixed = _TZ_OFFSET_RE.sub(r"\1\2:\3", value)
    try:
        return datetime.fromisoformat(fixed)
    except ValueError:
        return None
