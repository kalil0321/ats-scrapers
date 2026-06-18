"""SuperJob.ru — Russia's #2 job board after hh.ru.

SuperJob exposes a documented public REST API at
``api.superjob.ru/2.0/`` (docs: https://api.superjob.ru/). The
``/vacancies/`` endpoint requires a per-application secret in the
``X-Api-App-Id`` header — registration is free at
https://api.superjob.ru/register/. The scraper reads the secret from
the ``SUPERJOB_API_KEY`` env var; without it, ``fetch()`` raises a
``ScraperError`` pointing the operator at the registration page.

Like every Russian-internet job board, SuperJob geo-blocks non-CIS
IPs at the edge — even with a valid ``X-Api-App-Id``, requests from
US/EU datacenter ranges return ``403`` from ``server: sw`` (the
SuperJob edge) before the API ever sees them. Production runs must
route through a residential proxy with a CIS exit. Pass ``proxy_url``
explicitly or set the ``PROXY`` env var; both the standard
``http://user:pass@host:port`` URL form and the 4-colon
``host:port:user:pass`` shape some providers ship are accepted (same
helper jobs.ch / Programathor / hh.ru use).

The Evomi residential pool the rest of this project uses currently
exits in Chile / Vietnam — the request *reaches* SuperJob but the
API still rejects the missing app-id. This is the same operational
shape as the hh.ru scraper (PR #100): the code is correct, but a
CIS-exit residential proxy is required to actually populate the
dataset.

The "company" in ``company_slug`` is fixed to ``"superjob"`` since
SuperJob is a single-source job board (no per-tenant slugs).

API caps ``page * count`` at 500 results per filter combination, so
enumerating the whole corpus requires slicing the search space.
``slice_by`` controls the slicing strategy:

- ``"none"`` (default) — single query, capped at 500 results. Fine
  for tests and tiny markets.
- ``"town"`` — iterate the documented top-level town IDs (Moscow,
  Saint Petersburg, …). Cheapest fan-out with good coverage.

Doc reference: https://api.superjob.ru/
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

# --- API constants ----------------------------------------------------------

API_HOST = "api.superjob.ru"
API_VERSION = "2.0"
COUNTRY_ISO = "RU"
LANGUAGE = "ru"

# Per-app-id auth header. SuperJob's docs label it ``X-Api-App-Id``;
# the value is the per-application "secret_key" returned by the
# registration form at https://api.superjob.ru/register/.
ENV_API_KEY = "SUPERJOB_API_KEY"

# Per-page result count. Documented max is 100 for /vacancies/.
PER_PAGE = 100
# API hard cap: page * count <= 500 → at count=100, pages 0..4 inclusive.
MAX_PAGES_PER_SLICE = 5
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
DEFAULT_TIMEOUT = 30.0

# SuperJob wants a contact in the UA, same as hh.ru.
USER_AGENT = "jobhive/1.0 (kalil.bouzigues@gmail.com)"

# Top-level town IDs for slicing by city — taken from
# api.superjob.ru/2.0/towns/?all=1. Moscow + Saint Petersburg
# account for the majority of the corpus; the rest are densely-
# populated regional capitals where the slice keeps the result
# set under the 500-cap per query.
#
# References:
# - https://api.superjob.ru/#towns — towns endpoint (open, no auth)
# - The codes are stable; SuperJob hasn't renumbered since v2.0
#   shipped.
TOWNS: list[int] = [
    4,      # Moscow
    14,     # Saint Petersburg
    9,      # Yekaterinburg
    1118,   # Novosibirsk
    34,     # Nizhny Novgorod
    44,     # Kazan
    74,     # Rostov-on-Don
    78,     # Samara
    284,    # Krasnodar
    102,    # Krasnoyarsk
    1095,   # Volgograd
    1133,   # Voronezh
    49,     # Perm
    77,     # Saratov
    47,     # Ufa
    1136,   # Ulyanovsk
    1132,   # Tver
    1131,   # Vladimir
    2114,   # Sochi
    1119,   # Tyumen
]

# Map SuperJob ``type_of_work.title`` strings onto the canonical
# EmploymentType enum. SuperJob uses Russian labels; the values
# below are the exact strings the API returns (we match
# case-insensitively defensively). ``Сменный график`` (shift work)
# and ``Гибкий график`` (flexible hours) collapse to FULL_TIME —
# neither is a literal fit but FULL_TIME is the closest cross-ATS
# bucket consumers expect for "salaried but non-standard hours".
EMPLOYMENT_MAP: dict[str, str] = {
    "полный рабочий день": "FULL_TIME",
    "полная занятость": "FULL_TIME",
    "сменный график": "FULL_TIME",
    "гибкий график": "FULL_TIME",
    "частичная занятость": "PART_TIME",
    "неполный рабочий день": "PART_TIME",
    "временная работа": "TEMPORARY",
    "сезонная работа": "TEMPORARY",
    "стажировка": "INTERN",
    "вахтовый метод": "CONTRACT",
    "удаленная работа": "FULL_TIME",
}

# SuperJob ships a tiny ``currency`` set on /vacancies/ items —
# ``rub`` / ``uah`` / ``usd`` / ``eur`` / ``kzt`` / ``byn``. Normalize
# to ISO 4217 uppercase. Everything outside the whitelist is treated
# as "no signal" (the canonical schema needs an ISO code or nothing).
CURRENCY_MAP: dict[str, str] = {
    "rub": "RUB",
    "rur": "RUB",  # legacy alias, defensive
    "usd": "USD",
    "eur": "EUR",
    "uah": "UAH",
    "kzt": "KZT",
    "byn": "BYN",
    "byr": "BYN",  # legacy alias, defensive
}


def _resolve_proxy_url(raw: str | None) -> str | None:
    """Accept the 4-colon ``host:port:user:pass`` shape some
    residential-proxy providers ship and convert to the standard
    ``http://user:pass@host:port`` URL httpx expects. Plain
    ``http(s)://…`` URLs pass through.

    Mirrors the helper in ``hhru.py`` / ``programathor.py`` /
    ``jobsch.py``; kept local so the scraper has no internal
    cross-imports.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(?:https?://)?([^:/@]+):(\d+):([^:]+):(.+)$", raw)
    if m:
        host, port, user, pw = m.groups()
        safe_user = quote(user, safe="")
        safe_pw = quote(pw, safe="")
        return f"http://{safe_user}:{safe_pw}@{host}:{port}"
    return raw


@ScraperRegistry.register(ATSType.SUPERJOB)
class SuperJobScraper(BaseScraper):
    """SuperJob.ru scraper.

    ``company_slug`` is conventionally ``"superjob"`` — SuperJob is a
    single-source board so the slug is metadata, not a tenant key.
    Any value is accepted; it's only used to label the scraper.

    Knobs:

    - ``api_key`` — explicit SuperJob app secret. Falls back to the
      ``SUPERJOB_API_KEY`` env var. Required — without it the
      ``/vacancies/`` endpoint returns ``403`` with a Russian-language
      error pointing at the registration page.
    - ``proxy_url`` — explicit proxy URL. Falls back to ``PROXY`` env
      var (4-colon shape auto-converted), then to direct connection.
      SuperJob's edge returns ``403`` to US/EU datacenter IPs (even
      *with* a valid app-id) — pretty much always needed in production.
    - ``slice_by`` — slicing strategy to fan past the 500-result API
      cap. ``"none"`` (default) runs a single unsliced query;
      ``"town"`` iterates the town codes in :data:`TOWNS`.
    - ``towns`` — explicit override of the town code list, useful for
      testing or for narrowing the run to a single city.
    - ``max_pages_per_slice`` — pagination ceiling within one slice.
      Clamped to 5 (page 0..4 × count 100 = 500 results — the API's
      hard cap).
    """

    ats = ATSType.SUPERJOB

    def __init__(
        self,
        company_slug: str = "superjob",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        proxy_url: str | None = None,
        slice_by: str = "none",
        towns: list[int] | None = None,
        max_pages_per_slice: int = MAX_PAGES_PER_SLICE,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.api_key = api_key or os.environ.get(ENV_API_KEY, "").strip() or None
        self.proxy_url = _resolve_proxy_url(proxy_url) or _resolve_proxy_url(
            os.environ.get("PROXY")
        )
        if slice_by not in {"none", "town"}:
            raise ScraperError(
                f"SuperJobScraper: unsupported slice_by={slice_by!r}; "
                "expected 'none' or 'town'"
            )
        self.slice_by = slice_by
        self.towns = towns if towns is not None else TOWNS
        self.max_pages_per_slice = max(
            1, min(max_pages_per_slice, MAX_PAGES_PER_SLICE)
        )

    # --- public entry ------------------------------------------------------

    def fetch(self) -> list[Job]:
        if not self.api_key:
            raise ScraperError(
                f"{ENV_API_KEY} env var is required. SuperJob's /vacancies/ "
                "endpoint returns 403 without an X-Api-App-Id header. "
                "Register a free app at https://api.superjob.ru/register/ "
                "to obtain a secret_key."
            )
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

        async with httpx.AsyncClient(**client_kwargs) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def run_slice(slice_params: dict[str, str | int]) -> None:
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

    def _build_slices(self) -> list[dict[str, str | int]]:
        """Cartesian product of the active slicing dimensions.

        Kept dict-shaped so future ``slice_by="catalogue"`` /
        ``"keyword"`` modes can slot in without changing iteration
        code.
        """
        if self.slice_by == "none":
            return [{}]
        return [{"town": code} for code in self.towns]

    async def _iter_slice(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slice_params: dict[str, str | int],
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk pages within a single slice."""
        page = 0
        while page < self.max_pages_per_slice:
            payload = await self._fetch_page(client, sem, slice_params, page)
            items = payload.get("objects") or []
            if not items:
                return
            for item in items:
                yield item
            # SuperJob's pagination uses ``total`` + ``more`` — if
            # ``more`` is False the next page would be empty.
            if not payload.get("more"):
                return
            page += 1

    # --- HTTP --------------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slice_params: dict[str, str | int],
        page: int,
    ) -> dict[str, Any]:
        url = f"https://{API_HOST}/{API_VERSION}/vacancies/"
        params: dict[str, str | int] = {
            "count": PER_PAGE,
            "page": page,
            **slice_params,
        }
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Api-App-Id": self.api_key or "",
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with sem:
                    response = await client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"superjob fetch failed for {url} "
                        f"params={params}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    payload: dict[str, Any] = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"superjob returned non-JSON 200 for {url} "
                        f"params={params}: {exc}"
                    ) from exc
                return payload
            if response.status_code == 403:
                # Distinguish edge-level geo-block vs API-level missing
                # app-id by sniffing the response body — the edge ships
                # ``server: sw`` HTML or empty JSON; the API ships a
                # ``{"error":{...}}`` envelope with a Russian message.
                body_hint = (response.text or "")[:200]
                raise ScraperError(
                    f"superjob returned 403 for {url}. Either the "
                    f"{ENV_API_KEY} secret is missing/invalid or the "
                    "edge geo-blocked the request — production needs a "
                    "CIS-exit residential proxy (set PROXY env var). "
                    f"Body: {body_hint!r}"
                )
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"superjob returned {response.status_code} for "
                        f"{url} params={params} after {MAX_RETRIES} retries"
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
                f"superjob returned {response.status_code} for {url} "
                f"params={params}"
            )
        raise ScraperError(
            f"superjob exhausted retries for {url} params={params}: "
            f"{last_exc}"
        )

    # --- parsing -----------------------------------------------------------

    def _build_job(self, item: dict[str, Any]) -> Job | None:
        ats_id = item.get("id")
        url = item.get("link")
        title = item.get("profession")
        if not ats_id or not url or not title:
            return None

        # ``firm_name`` is the most common employer-name field; some
        # listings (especially anonymized "Конфиденциально" ones) only
        # populate ``client.title``. Keep both paths.
        company = item.get("firm_name") or ""
        if not company:
            client = item.get("client") or {}
            if isinstance(client, dict):
                company = client.get("title") or ""
        if not company:
            company = "Конфиденциально"  # "Confidential" — SuperJob's own label

        town = item.get("town") or {}
        location = town.get("title") if isinstance(town, dict) else None

        # SuperJob always returns ``payment_from`` / ``payment_to`` as
        # ints (0 when unknown — not None). Treat 0 as "no signal".
        payment_from_raw = item.get("payment_from")
        payment_to_raw = item.get("payment_to")
        payment_from = _as_positive_float(payment_from_raw)
        payment_to = _as_positive_float(payment_to_raw)
        currency_raw = item.get("currency")
        salary_currency: str | None = None
        salary_period: str | None = None
        salary_min: float | None = None
        salary_max: float | None = None
        if currency_raw and isinstance(currency_raw, str):
            mapped = CURRENCY_MAP.get(currency_raw.lower())
            if mapped and (payment_from is not None or payment_to is not None):
                salary_currency = mapped
                salary_period = "MONTH"  # SuperJob salaries are always monthly
                salary_min = payment_from
                salary_max = payment_to

        type_of_work = item.get("type_of_work") or {}
        type_title = (
            type_of_work.get("title") if isinstance(type_of_work, dict) else None
        )
        employment_type = _map_employment(type_title)
        commitment = type_title if type_title else None

        # Description: SuperJob ships ``candidat`` (requirements) +
        # ``vacancyRichText`` (HTML body) — combine them and strip
        # HTML for the canonical schema.
        description = _join_description(
            item.get("candidat"), item.get("vacancyRichText")
        )

        # ``date_published`` is a unix timestamp (seconds, UTC).
        posted_at = _parse_unix(item.get("date_published"))

        raw: dict[str, Any] = {}
        for key in (
            "experience",
            "education",
            "place_of_work",
            "agency",
            "moveable",
            "languages",
        ):
            v = item.get(key)
            if v is not None:
                raw[key] = v
        if isinstance(town, dict) and town.get("id"):
            raw["town_id"] = town["id"]
        category = item.get("catalogues")
        if category:
            raw["catalogues"] = category

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.SUPERJOB,
            ats_id=str(ats_id),
            location=location,
            country_iso=COUNTRY_ISO,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language=LANGUAGE,
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _as_positive_float(value: object) -> float | None:
    """SuperJob ships ``payment_from`` / ``payment_to`` as ints —
    ``0`` is the sentinel for "no value", not a literal zero salary.
    Drop 0 / negative / non-numeric values to ``None``."""
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if out <= 0:
        return None
    return out


def _map_employment(label: str | None) -> str | None:
    """Case-insensitive lookup against :data:`EMPLOYMENT_MAP`.

    SuperJob mixes capitalization between API responses (``"Полный
    рабочий день"`` vs ``"полный рабочий день"`` seen across the
    corpus); the map is keyed lowercase and the lookup folds case.
    """
    if not label:
        return None
    return EMPLOYMENT_MAP.get(label.strip().lower())


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _join_description(
    candidat: str | None, rich_text: str | None
) -> str | None:
    """SuperJob's ``candidat`` is plain text (requirements paragraph);
    ``vacancyRichText`` is HTML (full body). Combine, strip HTML,
    collapse whitespace."""
    parts = [p for p in (candidat, rich_text) if p]
    if not parts:
        return None
    joined = "\n".join(parts)
    cleaned = _HTML_TAG_RE.sub("", joined)
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _parse_unix(value: object) -> datetime | None:
    """``date_published`` is a unix timestamp in seconds (UTC).

    Defensive ``float()`` since the field has been observed as a
    stringified int on legacy listings."""
    if value is None:
        return None
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
