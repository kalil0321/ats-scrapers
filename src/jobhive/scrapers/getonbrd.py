"""Get on Board (https://www.getonbrd.com) — LATAM tech jobs scraper.

Get on Board is a tech-focused job board for Latin America. Companies post
directly (not aggregated from other sources), so coverage is small but
high-signal: ~1k active jobs across Argentina, Brazil, Chile, Colombia,
Mexico, Peru, Uruguay, plus remote roles open to LATAM applicants.

Public JSON:API at ``https://www.getonbrd.com/api/v0`` — no auth, no key.
The site-wide ``/jobs`` endpoint is auth-gated, but per-category
``/categories/{slug}/jobs`` is open. We enumerate the 18 categories the
``/categories`` endpoint advertises, paginate each (max
``per_page=120``), and resolve the embedded company / city /
modality references separately because the API doesn't support
``?include=`` for related resources.

The scraper is single-source: ``company_slug`` is informational and
ignored. Output rows carry the publishing employer's name as ``company``
so the publisher's cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.getonbrd.com/api/v0"
PER_PAGE = 120  # API hard-caps per_page; lower values just paginate more.
# Keep the request rate gentle — Get on Board returns 429 quickly when 18
# categories paginate concurrently AND each new job triggers an extra
# /companies/{id} fetch. Concurrency=3 + a longer backoff is enough to
# complete a full ~1k-job sweep in <60s without rate-limit drops.
MAX_CONCURRENCY = 3
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0

# Modality ``locale_key`` → canonical ``employment_type`` enum.
_MODALITY_MAP: dict[str, str] = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.GETONBRD)
class GetOnBrdScraper(BaseScraper):
    """Get on Board (getonbrd.com) — LATAM tech jobs.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"latam"``) — the scraper enumerates the entire
    site.
    """

    ats = ATSType.GETONBRD

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            categories = await self._list_categories(client, sem)

            # Lookup tables — one fetch each, then cached in memory for
            # the full run. Modalities is a tiny enum (~5-10 values);
            # cities are populated lazily as we encounter referenced IDs.
            modalities = await self._fetch_lookup(client, sem, "modalities")
            companies: dict[str, str] = {}
            cities: dict[str, dict[str, str]] = {}

            seen: set[str] = set()
            jobs: list[Job] = []

            async def per_category(slug: str) -> None:
                page = 1
                while True:
                    payload = await self._fetch_jobs_page(
                        client, sem, slug=slug, page=page,
                    )
                    items = payload.get("data") or []
                    for item in items:
                        ats_id = str(item.get("id") or "")
                        if not ats_id or ats_id in seen:
                            continue
                        seen.add(ats_id)
                        jobs.append(
                            await self._parse_job(
                                client, sem, item,
                                modalities=modalities,
                                companies=companies, cities=cities,
                            )
                        )
                    meta = payload.get("meta") or {}
                    if page >= int(meta.get("total_pages") or page):
                        return
                    page += 1

            await asyncio.gather(*(per_category(c) for c in categories))
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
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Get on Board fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Get on Board returned non-JSON for {url}: {exc}"
                    ) from exc
            if response.status_code == 404:
                # Treat 404 as "no such resource" — caller decides.
                return {}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Get on Board returned {response.status_code} for "
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
                f"Get on Board returned {response.status_code} for {url}"
            )
        raise ScraperError(f"Get on Board exhausted retries for {url}: {last_exc}")

    async def _list_categories(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore
    ) -> list[str]:
        payload = await self._request_json(client, sem, f"{API_ROOT}/categories")
        cats = [
            str(entry.get("id"))
            for entry in (payload.get("data") or [])
            if entry.get("id")
        ]
        if not cats:
            raise ScraperError("Get on Board /categories returned no entries")
        return cats

    async def _fetch_lookup(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        resource: str,
    ) -> dict[str, dict[str, str]]:
        """Fetch a small enum-style resource (modalities) and
        index it by id."""
        payload = await self._request_json(client, sem, f"{API_ROOT}/{resource}")
        return {
            str(entry.get("id")): entry.get("attributes") or {}
            for entry in (payload.get("data") or [])
            if entry.get("id")
        }

    async def _fetch_jobs_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        slug: str,
        page: int,
    ) -> dict[str, Any]:
        url = (
            f"{API_ROOT}/categories/{slug}/jobs"
            f"?per_page={PER_PAGE}&page={page}"
        )
        return await self._request_json(client, sem, url)

    async def _resolve_company(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        company_id: str,
        cache: dict[str, str],
    ) -> str:
        if company_id in cache:
            return cache[company_id]
        payload = await self._request_json(
            client, sem, f"{API_ROOT}/companies/{company_id}"
        )
        attrs = (payload.get("data") or {}).get("attributes") or {}
        name = (attrs.get("name") or "").strip() or company_id
        cache[company_id] = name
        return name

    async def _resolve_city(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        city_id: str,
        cache: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        if city_id in cache:
            return cache[city_id]
        payload = await self._request_json(
            client, sem, f"{API_ROOT}/cities/{city_id}"
        )
        attrs = (payload.get("data") or {}).get("attributes") or {}
        out = {
            "name": (attrs.get("name") or "").strip(),
            "country": (attrs.get("country") or "").strip(),
        }
        cache[city_id] = out
        return out

    # --- parsing ------------------------------------------------------------

    async def _parse_job(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        item: dict[str, Any],
        *,
        modalities: dict[str, dict[str, str]],
        companies: dict[str, str],
        cities: dict[str, dict[str, str]],
    ) -> Job:
        ats_id = str(item.get("id") or "")
        attrs = item.get("attributes") or {}
        title = _strip_html(attrs.get("title") or "Untitled")

        company_id = str(
            ((attrs.get("company") or {}).get("data") or {}).get("id") or ""
        )
        company = (
            await self._resolve_company(client, sem, company_id, companies)
            if company_id else "Unknown"
        )

        location = await self._format_location(
            client, sem, attrs, cities=cities,
        )

        modality_id = str(
            ((attrs.get("modality") or {}).get("data") or {}).get("id") or ""
        )
        modality_attrs = modalities.get(modality_id) or {}
        commitment = modality_attrs.get("name")
        employment_type = _MODALITY_MAP.get(
            (modality_attrs.get("locale_key") or "").lower()
        )

        description = _strip_html(_concat_descriptions(attrs))
        salary_min = _to_float(attrs.get("min_salary"))
        salary_max = _to_float(attrs.get("max_salary"))
        salary_currency = "USD" if (salary_min or salary_max) else None

        url = (item.get("links") or {}).get("public_url") or (
            f"https://www.getonbrd.com/jobs/{ats_id}"
        )

        raw: dict[str, Any] = {}
        for k in ("category_name", "lang", "perks", "remote_modality",
                  "remote_zone", "applications_count"):
            v = attrs.get(k)
            if v not in (None, "", []):
                raw[k] = v

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.GETONBRD,
            ats_id=ats_id,
            location=location,
            is_remote=bool(attrs.get("remote")),
            salary_currency=salary_currency,
            salary_period="MONTH",
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,
            commitment=commitment,
            department=attrs.get("category_name"),
            description=description,
            posted_at=_unix_to_dt(attrs.get("published_at")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )

    async def _format_location(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        attrs: dict[str, Any],
        *,
        cities: dict[str, dict[str, str]],
    ) -> str | None:
        # location_cities holds resolved-city refs; resolve the first one
        # we see (jobs rarely span more than one city in this dataset)
        # and fall back to the country list otherwise.
        city_refs = (
            ((attrs.get("location_cities") or {}).get("data")) or []
        )
        if city_refs:
            cid = str(city_refs[0].get("id") or "")
            if cid:
                resolved = await self._resolve_city(client, sem, cid, cities)
                name, country = resolved.get("name"), resolved.get("country")
                if name and country:
                    return f"{name}, {country}"
                return name or country
        countries = attrs.get("countries") or []
        if isinstance(countries, list) and countries:
            cleaned = [c for c in countries if isinstance(c, str) and c.strip()]
            if cleaned:
                return ", ".join(cleaned[:3])
        return None


def _to_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _unix_to_dt(value: object) -> datetime | None:
    """``published_at`` is a unix-epoch SECONDS integer."""
    try:
        sec = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if sec <= 0:
        return None
    return datetime.fromtimestamp(sec)


def _concat_descriptions(attrs: dict[str, Any]) -> str:
    """Get on Board splits description into ``description`` (requirements),
    ``functions`` (responsibilities), ``projects`` (about the role) and a
    couple of optional sections. Concatenate the populated ones into a
    single body."""
    parts: list[str] = []
    for key in ("projects", "functions", "description", "desirable", "benefits"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n\n".join(parts)


def _strip_html(text: str) -> str:
    """Strip tags + entities + collapse whitespace, then truncate so the
    canonical schema's ~25k chars description budget isn't blown by long
    HTML-formatted bodies."""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:25_000]
