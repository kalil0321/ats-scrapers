"""BreezyHR careers scraper.

BreezyHR exposes a single public JSON endpoint per tenant:

    GET https://{slug}.breezy.hr/json

Returns ``[{"id":..., "name":..., "location":..., ...}]`` — every position
in one response, no pagination. Each position carries title, location
(structured city/state/country with remote flag), department, salary
range, full-time/part-time type, and the canonical job URL.

The list endpoint does NOT include the job description. Each detail
page (``/p/{id}-{slug}``) is server-rendered HTML with the body in a
``<div class="description">`` block — we fetch it concurrently per job.

Tenants without an active Breezy careers site return a 302 redirect to
``https://breezy.hr/`` (the marketing site) — we treat that as
``CompanyNotFoundError``. Tenants with an active site but zero open
positions return a 200 with ``[]`` (handled cleanly).

Note: BreezyHR's older v3 API (``api.breezy.hr/v3/...``) is OAuth-gated.
This scraper uses only the public unauthenticated endpoint.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers import fetch as fetch_layer
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_TEMPLATE = "https://{slug}.breezy.hr/json"
# Per-tenant concurrent detail-page fetches. Tenants are usually
# small (<50 jobs). Keep this low — Breezy fronts every tenant on a
# shared CF/Akamai-style edge which 403-blocks bursty traffic. Empirically
# we got blocked at ~14 req/s during a 685-tenant pass with cross-tenant
# concurrency 8 + per-tenant 6; 4 keeps us under the threshold.
DETAIL_CONCURRENCY = 4

# The listing fetcher runs with ``follow_redirects=False`` so the
# 302→marketing-site bounce is visible; redirect statuses come back
# unmapped for the scraper to turn into ``CompanyNotFoundError``.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 403 on the listing is edge-rate-limit (CF/Akamai-style) rather than a
# real auth failure — the Fetcher would treat it as a hard block, so the
# scraper takes it back unmapped and retries with backoff itself.
_LISTING_HANDLED = _REDIRECT_STATUSES | {403}
# Detail-page fetches are best-effort: any error status just keeps the
# listing-derived row.
_DETAIL_HANDLED = frozenset(range(400, 600))

_TYPE_MAP = {
    "fullTime": "FULL_TIME",
    "partTime": "PART_TIME",
    "contract": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.BREEZY)
class BreezyScraper(BaseScraper):
    """BreezyHR scraper. ``company_slug`` is the tenant subdomain
    (e.g. ``"fathom"`` → ``https://fathom.breezy.hr/json``)."""

    ats = ATSType.BREEZY

    default_headers: ClassVar[dict[str, str] | None] = {"User-Agent": "Mozilla/5.0"}

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = require_host_label(company_slug, provider="BreezyScraper")

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(fetch, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        # ``follow_redirects=False`` on the listing fetcher — the JSON
        # listing endpoint must NOT follow redirects so we can detect
        # the 302→marketing-site bounce as ``CompanyNotFoundError``.
        async with self.make_fetcher(follow_redirects=False) as fetch:
            payload = await self._fetch_listing(fetch)
        if not isinstance(payload, list):
            raise ScraperError(
                f"BreezyHR returned non-list JSON for {self.company_slug}"
            )
        seen: set[str] = set()
        jobs: list[Job] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            job = self._parse_position(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)

        # Detail-page enrichment is best-effort. Breezy's edge blocks
        # bursty traffic with 403s, so per-job failures keep the
        # listing-derived row instead of failing the tenant. Detail
        # pages redirect legitimately, so this fetcher follows them.
        if self.include_descriptions and jobs:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_description(fetch, sem, j) for j in jobs
                ))
        return jobs

    async def _enrich_description(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await fetch.request(
                    "GET", str(job.url), handled=_DETAIL_HANDLED,
                )
            except ScraperError:
                return
        if response.status_code != 200:
            return
        description = _extract_description(response.text)
        if description:
            job.description = description[:25_000]

    async def _fetch_listing(self, fetch: Fetcher) -> list[dict[str, Any]]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        # The Fetcher covers 404/429/5xx and network retries; redirects
        # and the 403-means-rate-limited quirk are handled here. The 403
        # retry loop reads the shared fetch-layer defaults at call time
        # (same knobs the suite-wide conftest fixture zeroes).
        retries = fetch_layer.DEFAULT_RETRIES
        for attempt in range(1, retries + 1):
            response = await fetch.request(
                "GET",
                url,
                headers={"Accept": "application/json"},
                handled=_LISTING_HANDLED,
            )
            if response.status_code in _REDIRECT_STATUSES:
                # The slug doesn't have an active Breezy careers site —
                # Breezy redirects to its marketing site.
                raise CompanyNotFoundError(
                    f"BreezyHR tenant has no active careers site: {self.company_slug}"
                )
            if response.status_code == 403:
                # Edge-rate-limit (CF/Akamai-style) rather than a real
                # auth failure — Breezy's public JSON endpoint is
                # unauthenticated. Treat it as transient and back off.
                if attempt == retries:
                    raise ScraperError(
                        f"BreezyHR returned 403 for {self.company_slug} "
                        f"after {retries} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else min(
                        fetch_layer.DEFAULT_RETRY_BASE_DELAY * 2 ** (attempt - 1),
                        fetch_layer.DEFAULT_MAX_RETRY_DELAY,
                    )
                )
                await asyncio.sleep(delay)
                continue
            try:
                return response.json()
            except ValueError as exc:
                raise ScraperError(
                    f"BreezyHR returned malformed JSON for {self.company_slug}: {exc}"
                ) from exc
        raise ScraperError(f"BreezyHR exhausted retries for {self.company_slug}")

    def _parse_position(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("name") or "").strip()
        url = item.get("url")
        if not ats_id or not title or not url:
            return None

        type_info = item.get("type")
        type_id = type_info.get("id") if isinstance(type_info, dict) else None
        employment_type = _TYPE_MAP.get(str(type_id)) if type_id else None

        company_info = item.get("company") or {}
        company_name = (
            company_info.get("name")
            if isinstance(company_info, dict) and company_info.get("name")
            else self.company_slug
        )

        raw: dict[str, Any] = {}
        for k in ("category", "experience", "education", "tags"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=title,
            company=company_name,
            ats_type=ATSType.BREEZY,
            ats_id=ats_id,
            location=_format_location(item.get("location")),
            is_remote=_extract_is_remote(item.get("location")),
            department=item.get("department") or None,
            salary_summary=item.get("salary") or None,
            employment_type=employment_type,
            posted_at=_parse_iso(item.get("published_date")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _extract_description(html: str) -> str | None:
    """Pull the plain-text description body from a Breezy detail page.

    Breezy renders the position body as ``<div class="description">``
    (rich HTML — paragraphs, lists). Tenants that hide the description
    behind a login or whose pages lack the standard markup yield
    ``None``; the caller silently keeps the listing-derived row.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ScraperError(
            "BreezyHR detail-page enrichment requires beautifulsoup4."
        ) from exc

    soup = BeautifulSoup(html, "html.parser")
    block = soup.find(class_="description")
    if block is None:
        return None
    text = block.get_text(separator="\n", strip=True)
    return text or None


def _format_location(value: object) -> str | None:
    """Breezy's location is structured: ``{"city": ..., "state": {"name": ...},
    "country": {"name": ...}}`` plus a pre-built ``name`` field. Prefer the
    pre-built name when present."""
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    parts: list[str] = []
    city = value.get("city")
    if isinstance(city, str) and city.strip():
        parts.append(city.strip())
    state = value.get("state")
    if isinstance(state, dict):
        sn = state.get("name") or state.get("id")
        if isinstance(sn, str) and sn.strip():
            parts.append(sn.strip())
    country = value.get("country")
    if isinstance(country, dict):
        cn = country.get("name")
        if isinstance(cn, str) and cn.strip():
            parts.append(cn.strip())
    return ", ".join(parts) or None


def _extract_is_remote(value: object) -> bool | None:
    if not isinstance(value, dict):
        return None
    flag = value.get("is_remote")
    if isinstance(flag, bool):
        return flag
    return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
