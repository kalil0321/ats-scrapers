"""Cornerstone OnDemand careers scraper.

Used by American Express, JetBlue, Henkel, T-Mobile, and many others.

Cornerstone career sites live at the pattern:
    https://{slug}.csod.com/ux/ats/careersite/{site_id}/home?c={slug}

The flow is two-step:

1. GET the career site HTML and extract a JWT token (`csod.context.token`)
   plus the regional API host (one of ``na.api.csod.com``, ``eu-fra.api.csod.com``,
   ``uk.api.csod.com``, ...).

2. POST to ``{api_host}/rec-job-search/external/jobs`` with the JWT as a
   Bearer token to retrieve the job list. The response includes
   ``totalCount`` and ``requisitions`` per page; we paginate via ``pageNumber``
   until we've collected everything.

JWT tokens expire after ~1 hour, but typical scrapes finish well within that.
Rate limit: ~60 req/min — we use MAX_CONCURRENCY=4 with the global retry policy.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

PAGE_SIZE = 25
MAX_CONCURRENCY = 4  # Cornerstone rate-limits ~60 req/min
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_TOKEN_RE = re.compile(
    r'csod\.context\.token\s*=\s*[\'"]([^\'"]+)[\'"]'
)
_TOKEN_FALLBACK_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')
_API_HOST_RE = re.compile(r'(https?://[a-z0-9-]+\.api\.csod\.com)')

_DEFAULT_API_HOST = "https://na.api.csod.com"
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.CORNERSTONE)
class CornerstoneScraper(BaseScraper):
    """Cornerstone scraper. ``company_slug`` can be either a bare slug
    (``"henkel"`` → ``https://henkel.csod.com/ux/ats/careersite/1/home?c=henkel``)
    or the full career-site URL.

    ``site_id``: the numeric career-site ID (Henkel uses 1, TheKids uses 4).
    Defaults to ``1``; override per tenant when known."""

    ats = ATSType.CORNERSTONE

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        site_id: int = 1,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.site_id = site_id
        self.career_url, self.slug = _resolve_career_url(company_slug, site_id)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            token, api_host = await self._init_session(client)
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            first = await self._search(
                client, sem, token=token, api_host=api_host, page=1
            )
            data = first.get("data") or {}
            total = int(data.get("totalCount") or 0)
            requisitions = data.get("requisitions") or []

            seen: set[str] = set()
            all_jobs: list[Job] = []

            def absorb(reqs: list[dict[str, Any]]) -> None:
                for item in reqs:
                    job = self._parse_requisition(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    all_jobs.append(job)

            absorb(requisitions)

            if total > len(requisitions):
                # Fan out remaining pages.
                last_page = (total + PAGE_SIZE - 1) // PAGE_SIZE

                async def task(page: int) -> None:
                    payload = await self._search(
                        client, sem, token=token, api_host=api_host, page=page
                    )
                    absorb((payload.get("data") or {}).get("requisitions") or [])

                await asyncio.gather(
                    *(task(p) for p in range(2, last_page + 1))
                )
        return all_jobs

    async def _init_session(
        self, client: httpx.AsyncClient
    ) -> tuple[str, str]:
        try:
            response = await client.get(
                self.career_url, headers={"User-Agent": "Mozilla/5.0"}
            )
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"Cornerstone init failed for {self.career_url}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"Cornerstone career site not found: {self.career_url}"
            )
        if response.status_code != 200:
            raise ScraperError(
                f"Cornerstone init returned {response.status_code} for {self.career_url}"
            )
        text = response.text
        match = _TOKEN_RE.search(text) or _TOKEN_FALLBACK_RE.search(text)
        if not match:
            raise ScraperError(
                f"Cornerstone: couldn't extract JWT token from {self.career_url}. "
                f"The career-site page format may have changed."
            )
        token = match.group(1)
        host_match = _API_HOST_RE.search(text)
        api_host = host_match.group(1) if host_match else _DEFAULT_API_HOST
        return token, api_host

    async def _search(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        token: str,
        api_host: str,
        page: int,
    ) -> dict[str, Any]:
        url = f"{api_host}/rec-job-search/external/jobs"
        body = {
            "careerSiteId": self.site_id,
            "careerSitePageId": self.site_id,
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "cultureId": 1,  # English
            "cultureName": "en-US",
        }
        career_origin = f"https://{urlparse(self.career_url).hostname}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": career_origin,
            "Referer": career_origin + "/",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Cornerstone search failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Cornerstone returned malformed JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code in (429, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Cornerstone returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Cornerstone returned {response.status_code} at page={page}: "
                f"{response.text[:120]}"
            )
        raise ScraperError(f"Cornerstone exhausted retries at page={page}")

    def _parse_requisition(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("requisitionId") or "")
        if not ats_id:
            return None
        title = (item.get("displayJobTitle") or "").strip() or "Untitled"
        career_origin = f"https://{urlparse(self.career_url).hostname}"
        url = f"{career_origin}/ux/ats/careersite/{self.site_id}/job/{ats_id}?c={self.slug}"

        raw: dict[str, Any] = {}
        for k in ("jobType", "schedule", "shift", "department",
                  "industry", "category", "experienceLevel"):
            v = item.get(k)
            if v:
                raw[k] = v

        # Cornerstone uses ``requisitionId`` as both the URL key and a stable
        # employer-side identifier — surface as requisition_id even though it
        # also doubles as ats_id in the URL pattern.
        return Job(
            url=url,
            title=title,
            company=self.slug,
            ats_type=ATSType.CORNERSTONE,
            ats_id=ats_id,
            location=_format_locations(item.get("locations")),
            commitment=item.get("schedule") if isinstance(item.get("schedule"), str) else None,
            requisition_id=ats_id if ats_id else None,
            description=_clean_description(item.get("externalDescription")),
            posted_at=_parse_iso(item.get("postingEffectiveDate")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _resolve_career_url(slug_or_url: str, site_id: int) -> tuple[str, str]:
    """Return ``(career_url, slug)``. Accepts a bare slug or the full URL."""
    if slug_or_url.startswith(("http://", "https://")):
        # Try to extract slug from the URL's `?c=` query param or hostname.
        m = re.search(r"[?&]c=([^&#]+)", slug_or_url)
        if m:
            slug = m.group(1)
        else:
            host = urlparse(slug_or_url).hostname or ""
            slug = host.split(".")[0] if host else slug_or_url
        return slug_or_url, slug
    slug = slug_or_url
    return (
        f"https://{slug}.csod.com/ux/ats/careersite/{site_id}/home?c={slug}",
        slug,
    )


def _format_locations(value: object) -> str | None:
    """Cornerstone returns ``locations`` as a list of dicts with city, state,
    country fields. We flatten the first one to ``"City, State, Country"``."""
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if isinstance(first, str):
        return first.strip() or None
    if not isinstance(first, dict):
        return None
    parts = [
        first.get(k) for k in ("city", "state", "country")
        if isinstance(first.get(k), str) and first.get(k, "").strip()
    ]
    if parts:
        return ", ".join(p.strip() for p in parts if p)
    name = first.get("name") or first.get("displayName")
    return name.strip() if isinstance(name, str) and name.strip() else None


# Cornerstone tenants who haven't filled in their public description leave
# the field as a placeholder string. Filter those out — better an empty
# description column than a misleading "Please upload" everywhere.
_PLACEHOLDER_DESCRIPTIONS = {
    "please upload the job description",
    "please upload a job description",
    "please add the job description",
    "no description available",
    "to be confirmed",
    "tbc",
    "used for itt applications",
    "n/a",
    "tba",
    "see job description",
}


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _TAG_RE.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if cleaned.lower() in _PLACEHOLDER_DESCRIPTIONS:
        return None
    return cleaned[:25_000]


def _parse_iso(value: object) -> datetime | None:
    """Parse Cornerstone's posted-date field.

    The API ships ``postingEffectiveDate`` as ``M/D/YYYY`` (US locale,
    e.g. ``"5/6/2026"``). ISO 8601 is the fallback for tenants on
    non-US locales — we try it first because it's the cheaper parse,
    then fall through to the localized US format.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None
