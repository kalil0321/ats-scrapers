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

The search answers in one culture (language and region) at a time. Recruiters
fill the advert's title and text in their site's culture, and the other
cultures keep the form's default text (``<<ENTER DISPLAY JOB TITLE>>``,
``Display job title``...). The career site HTML names its own culture next to
the token (``"cultureID":2,"cultureName":"en-GB"``), so the search asks for
that one, and for US English when the page names none. A requisition whose
title is still unfilled in the site's culture is read once more in US English.

JWT tokens expire after ~1 hour, but typical scrapes finish well within that.
Rate limit: ~60 req/min — we use MAX_CONCURRENCY=4 with the global retry policy.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

PAGE_SIZE = 25
MAX_CONCURRENCY = 4  # Cornerstone rate-limits ~60 req/min

_TOKEN_RE = re.compile(
    r'csod\.context\.token\s*=\s*[\'"]([^\'"]+)[\'"]'
)
_TOKEN_FALLBACK_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')
_API_HOST_RE = re.compile(r'(https?://[a-z0-9-]+\.api\.csod\.com)')

_DEFAULT_API_HOST = "https://na.api.csod.com"
# The culture pair as the career site writes it in ``csod.context``.
_CULTURE_ID_RE = re.compile(r'"cultureID"\s*:\s*"?(\d{1,5})"?')
_CULTURE_NAME_RE = re.compile(
    r'"cultureName"\s*:\s*"([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3})"'
)
_DEFAULT_CULTURE: tuple[int, str] = (1, "en-US")
# A date as the search prints it: three numbers and the culture's punctuation
# (``11/6/2026``, ``24.08.2026``, ``13. 11. 2026.``, ``2026-11-12``).
_NUMERIC_DATE_RE = re.compile(r"^\D*(\d{1,4})\D+(\d{1,2})\D+(\d{1,4})\D*$")
# Cultures seen to print the month before the day. Used only when a listing
# holds no date that settles the order by itself.
_MONTH_FIRST_CULTURES = frozenset({"en-us", "ca-es"})
_EARLIEST_POSTING_YEAR = 1990
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
        company_name: str | None = None,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        # Full URLs containing a career-site ID take precedence over site_id.
        self.career_url, self.slug, resolved_site_id = _resolve_career_url(
            company_slug, site_id
        )
        self.site_id = resolved_site_id
        self.company_name = (
            company_name.strip()
            if company_name and company_name.strip()
            else self.slug
        )

    default_headers: ClassVar[dict[str, str]] = {"User-Agent": "Mozilla/5.0"}

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            token, api_host, culture = await self._init_session(fetch)
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            items = await self._list(
                fetch, sem, token=token, api_host=api_host, culture=culture
            )
            if culture != _DEFAULT_CULTURE and any(
                _title_unfilled(item) for item in items
            ):
                items = await self._fill_from_default_culture(
                    fetch, sem, token=token, api_host=api_host, items=items
                )

        seen: set[str] = set()
        all_jobs: list[Job] = []
        for item in items:
            job = self._parse_requisition(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            all_jobs.append(job)
        return all_jobs

    async def _list(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        token: str,
        api_host: str,
        culture: tuple[int, str],
    ) -> list[dict[str, Any]]:
        """Every requisition of the site in one culture, in page order."""
        first = await self._search(
            fetch, sem, token=token, api_host=api_host, culture=culture, page=1
        )
        data = first.get("data") or {}
        total = int(data.get("totalCount") or 0)
        items: list[dict[str, Any]] = list(data.get("requisitions") or [])
        if total > len(items):
            # Fan out remaining pages.
            last_page = (total + PAGE_SIZE - 1) // PAGE_SIZE

            async def task(page: int) -> list[dict[str, Any]]:
                payload = await self._search(
                    fetch,
                    sem,
                    token=token,
                    api_host=api_host,
                    culture=culture,
                    page=page,
                )
                return (payload.get("data") or {}).get("requisitions") or []

            for page_items in await asyncio.gather(
                *(task(p) for p in range(2, last_page + 1))
            ):
                items.extend(page_items)
        return _iso_posting_dates(items, culture[1])

    async def _fill_from_default_culture(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        token: str,
        api_host: str,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Take the US English advert where the site's culture has none.

        Some tenants write part of their adverts in US English only, whatever
        culture their site opens in. Only requisitions whose title is unfilled
        change, and only when the US English one is filled; the advert is
        taken whole so that title and text stay in one language.
        """
        try:
            english = await self._list(
                fetch, sem, token=token, api_host=api_host, culture=_DEFAULT_CULTURE
            )
        except ScraperError as exc:
            logger.warning(
                "Cornerstone %s: US English listing failed, keeping unfilled "
                "titles of the site's culture: %s",
                self.career_url,
                exc,
            )
            return items
        by_id = {
            str(item.get("requisitionId")): item
            for item in english
            if not _title_unfilled(item)
        }
        return [
            by_id.get(str(item.get("requisitionId")), item)
            if _title_unfilled(item)
            else item
            for item in items
        ]

    async def _init_session(
        self, fetch: Fetcher
    ) -> tuple[str, str, tuple[int, str]]:
        text = await fetch.get_text(self.career_url)
        match = _TOKEN_RE.search(text) or _TOKEN_FALLBACK_RE.search(text)
        if not match:
            raise ScraperError(
                f"Cornerstone: couldn't extract JWT token from {self.career_url}. "
                f"The career-site page format may have changed."
            )
        token = match.group(1)
        host_match = _API_HOST_RE.search(text)
        api_host = host_match.group(1) if host_match else _DEFAULT_API_HOST
        return token, api_host, _site_culture(text)

    async def _search(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        *,
        token: str,
        api_host: str,
        culture: tuple[int, str] = _DEFAULT_CULTURE,
        page: int,
    ) -> dict[str, Any]:
        url = f"{api_host}/rec-job-search/external/jobs"
        body = {
            "careerSiteId": self.site_id,
            "careerSitePageId": self.site_id,
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "cultureId": culture[0],
            "cultureName": culture[1],
        }
        career_origin = f"https://{urlparse(self.career_url).hostname}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": career_origin,
            "Referer": career_origin + "/",
        }
        async with sem:
            response = await fetch.request("POST", url, json=body, headers=headers)
        try:
            return response.json()
        except ValueError as exc:
            raise ScraperError(
                f"Cornerstone returned malformed JSON at page={page}: {exc}"
            ) from exc

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
            company=self.company_name,
            ats_type=ATSType.CORNERSTONE,
            ats_id=ats_id,
            location=_format_locations(item.get("locations")),
            commitment=item.get("schedule") if isinstance(item.get("schedule"), str) else None,
            requisition_id=ats_id if ats_id else None,
            description=_clean_description(item.get("externalDescription")),
            posted_at=_parse_iso(item.get("postingEffectiveDate")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _iso_posting_dates(
    items: list[dict[str, Any]], culture_name: str
) -> list[dict[str, Any]]:
    """Rewrite each ``postingEffectiveDate`` of one listing as ``YYYY-MM-DD``.

    The search prints the date the way the culture asked for writes it, so
    ``05/11/2026`` is 11 May on a US site and 5 November on a British one. The
    listing settles the order when one of its dates has a number above 12 in
    the day's place; otherwise the culture does. A date that cannot be read,
    or whose year is not a plausible posting year (another calendar), becomes
    ``None`` instead of a guess. Other values (ISO timestamps) are left as
    they are.
    """
    matches = [
        _NUMERIC_DATE_RE.match(value)
        if isinstance(value := item.get("postingEffectiveDate"), str)
        else None
        for item in items
    ]
    year_last = [
        (int(match.group(1)), int(match.group(2)))
        for match in matches
        if match is not None and len(match.group(3)) == 4
    ]
    day_first_seen = any(first > 12 for first, _ in year_last)
    month_first_seen = any(second > 12 for _, second in year_last)
    if day_first_seen != month_first_seen:
        day_first = day_first_seen
    else:
        day_first = culture_name.casefold() not in _MONTH_FIRST_CULTURES
    return [
        item
        if match is None
        else {**item, "postingEffectiveDate": _iso_date(match, day_first=day_first)}
        for item, match in zip(items, matches, strict=True)
    ]


def _iso_date(match: re.Match[str], *, day_first: bool) -> str | None:
    first, second, third = match.groups()
    if len(first) == 4:
        year, month, day = first, second, third
    elif len(third) == 4:
        year = third
        day, month = (first, second) if day_first else (second, first)
    else:
        return None
    if not _EARLIEST_POSTING_YEAR <= int(year) <= datetime.now(UTC).year + 1:
        return None
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _title_unfilled(item: dict[str, Any]) -> bool:
    """True when the advert's title was never written in the culture asked for.

    The search then answers nothing, the text ``null``, or the form's default,
    which Cornerstone wraps in angle brackets (``<<ENTER DISPLAY JOB TITLE>>``,
    sent as ``&lt;&lt;...``). No job title starts with an angle bracket.
    """
    title = item.get("displayJobTitle")
    if not isinstance(title, str):
        return True
    title = title.strip()
    return not title or title.casefold() == "null" or title.startswith(("<", "&lt;"))


def _site_culture(page: str) -> tuple[int, str]:
    """The culture the career site serves, as an (id, name) pair.

    Both halves come from the page or neither does: an id sent with another
    culture's name would ask the search for a pair the site never set up.
    """
    culture_id = _CULTURE_ID_RE.search(page)
    culture_name = _CULTURE_NAME_RE.search(page)
    if culture_id is None or culture_name is None:
        return _DEFAULT_CULTURE
    return int(culture_id.group(1)), culture_name.group(1)


def _resolve_career_url(slug_or_url: str, site_id: int) -> tuple[str, str, int]:
    """Return ``(career_url, slug, site_id)``.

    Accepts a bare slug or the full URL. Full URLs may point at non-default
    career sites such as ``/careersite/3/home``; keep that site id for API
    requests instead of silently using the constructor default.
    """
    if slug_or_url.startswith(("http://", "https://")):
        # Full URL (custom career site): validate the target host before
        # it becomes a fetch target — bare slugs land in {slug}.csod.com.
        slug_or_url = require_http_url(slug_or_url, provider="CornerstoneScraper")
        # Try to extract slug from the URL's `?c=` query param or hostname.
        m = re.search(r"[?&]c=([^&#]+)", slug_or_url)
        if m:
            slug = m.group(1)
        else:
            host = urlparse(slug_or_url).hostname or ""
            slug = host.split(".")[0] if host else slug_or_url
        site_match = re.search(r"/careersite/(\d+)/", slug_or_url)
        resolved_site_id = int(site_match.group(1)) if site_match else site_id
        return slug_or_url, slug, resolved_site_id
    slug = require_host_label(slug_or_url, provider="CornerstoneScraper")
    return (
        f"https://{slug}.csod.com/ux/ats/careersite/{site_id}/home?c={slug}",
        slug,
        site_id,
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
    """Parse a posting date that :func:`_iso_posting_dates` has made ISO 8601.

    Tenants that already send an ISO timestamp pass through the same parse.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
