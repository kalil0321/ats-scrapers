"""ROAM Africa — multi-country job-board scraper.

ROAM Africa runs a single Laravel/Tailwind job-board template across
five African country sites:

- Jobberman Nigeria   — https://www.jobberman.com
- BrighterMonday Kenya — https://www.brightermonday.co.ke
- BrighterMonday Uganda — https://www.brightermonday.co.ug
- BrighterMonday Tanzania — https://www.brightermonday.co.tz
- Jobberman Ghana    — https://www.jobberman.com.gh

All five share the same listing-card markup (``data-cy="listing-cards-components"``
on each card, ``aria-labelledby="job-{numeric-id}-title"`` carries the
internal listing id, salary tag spelled ``{ISO-currency} {min} - {max}``).
A single scraper class therefore covers the entire network — the
``company_slug`` constructor argument selects which region to crawl.

Pagination is page-number based (``?page=N``). Each page returns ~16
job cards plus a few "featured" listings repeated at the top of every
page (we dedup by listing id). We stop when a page yields zero new
ids OR when we hit a 404 OR the safety cap of 500 pages.

The detail-page route ``/listings/{slug}`` embeds a structured
``<script type="application/ld+json">`` ``JobPosting`` schema with
title / description / salary / employment_type / industry /
``datePosted`` — strictly richer than the listing card. We don't
fetch detail pages by default (would 10x the request count for ~3k
Nigerian jobs) but the JSON-LD parsing is exposed so a downstream
enrichment pipeline can opt-in.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)


# Region map: company_slug → (base_url, country_iso, language).
# Currency is derived from country_iso via _COUNTRY_CURRENCY below.
REGIONS: dict[str, tuple[str, str, str]] = {
    "jobberman-ng": ("https://www.jobberman.com", "NG", "en"),
    "brightermonday-ke": ("https://www.brightermonday.co.ke", "KE", "en"),
    "brightermonday-ug": ("https://www.brightermonday.co.ug", "UG", "en"),
    "brightermonday-tz": ("https://www.brightermonday.co.tz", "TZ", "en"),
    "jobberman-gh": ("https://www.jobberman.com.gh", "GH", "en"),
}

# Hard fallback: same id space the legacy `/listings/{slug}` URL uses.
_COUNTRY_CURRENCY: dict[str, str] = {
    "NG": "NGN",
    "KE": "KES",
    "UG": "UGX",
    "TZ": "TZS",
    "GH": "GHS",
}

# Display label on the card ↔ canonical employment_type enum.
_EMPLOYMENT_MAP: dict[str, str] = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
}

# JSON-LD also uses the canonical enum spelling directly. Map them all
# to our schema's accepted values.
_JSONLD_EMPLOYMENT_MAP: dict[str, str] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "CONTRACT": "CONTRACT",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
    "TEMPORARY": "TEMPORARY",
}

DEFAULT_MAX_PAGES = 500
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Listing card boundary — one card per ``aria-labelledby="job-{id}-title"``.
_CARD_RE = re.compile(
    r'aria-labelledby="job-(?P<id>\d+)-title"\s*>(?P<body>.*?)(?='
    r'aria-labelledby="job-\d+-title"|</main>|<footer)',
    re.DOTALL,
)
# The listing-card title anchor — ROAM templates put attributes in
# different orders depending on whether the card is "featured" or
# regular (featured anchors lead with ``href``; regular ones lead with
# ``data-cy``). Match the whole opening tag, then pluck attributes off
# its body so order doesn't matter.
_TITLE_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?data-cy="listing-title-link"[^>]*?>',
    re.DOTALL,
)
_HREF_ATTR_RE = re.compile(r'href="(?P<url>[^"]+/listings/[^"]+)"')
_TITLE_ATTR_RE = re.compile(r'title="(?P<title>[^"]+)"')
# Company sits in a teaser <p> right after the title <a>. The site
# class-bombs every node so we anchor on ``text-link-700`` /
# ``text-link-blue`` which are the two variants observed in the wild
# (jobberman uses ``text-blue-700`` for company display).
_COMPANY_RE = re.compile(
    r'class="[^"]*(?:text-blue-700|text-link-700|text-link-blue)[^"]*"'
    r'[^>]*>\s*([^<]+?)\s*</p>',
    re.DOTALL,
)
# The salary chip is an ISO-3 currency code followed by the amount
# ("Commission" / "Confidential" for non-numeric chips). The amount is
# sometimes wrapped in an inner ``<span>`` (raw card HTML) and sometimes
# bare (cleaned plain text), so the wrapper is optional and the body
# stops at the next tag or end of string. This lets the same pattern
# match both the raw card body and a ``_clean_text``-ed badge.
_SALARY_RE = re.compile(
    r'(?P<curr>NGN|KES|UGX|TZS|GHS|USD)\s*(?:<span[^>]*>\s*)?'
    r'(?P<body>[^<]+?)\s*(?:</span>|<|\Z)',
    re.DOTALL,
)
# Employment-type chip — match the badge classes used by the template.
_BADGE_RE = re.compile(
    r'class="[^"]*bg-brand-secondary-100[^"]*"[^>]*>\s*([^<]+?)\s*</span>',
    re.DOTALL,
)
_TIME_AGO_RE = re.compile(
    r'(\d+)\s+(hour|day|week|month|year)s?\s+ago',
    re.IGNORECASE,
)
# Detail-page JSON-LD: any ``<script type="application/ld+json">`` block.
_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


@ScraperRegistry.register(ATSType.ROAMAFRICA)
class RoamAfricaScraper(BaseScraper):
    """ROAM Africa multi-country job-board scraper.

    The ``company_slug`` constructor argument selects which country
    site to crawl — see :data:`REGIONS` for the supported keys
    (``jobberman-ng``, ``brightermonday-ke``, ``brightermonday-ug``,
    ``brightermonday-tz``, ``jobberman-gh``).

    Knobs:

    - ``max_pages`` — pagination ceiling, defaults to 500. Real
      networks top out around 250 pages on Nigeria; smaller countries
      finish in <30. The scraper stops earlier if a page returns no
      new ids twice in a row.
    """

    ats = ATSType.ROAMAFRICA

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        if company_slug not in REGIONS:
            raise ScraperError(
                f"Unknown ROAM Africa region {company_slug!r}. "
                f"Supported: {sorted(REGIONS)}"
            )
        self.max_pages = max_pages
        self._base_url, self._country_iso, self._language = REGIONS[company_slug]
        self._currency = _COUNTRY_CURRENCY[self._country_iso]

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 2:
                try:
                    page_jobs = await self._fetch_listing_page(client, sem, page)
                except ScraperError:
                    if page == 1:
                        raise
                    log.warning(
                        "ROAM Africa %s: stopping pagination at page %d; "
                        "keeping %d jobs collected so far.",
                        self.company_slug, page, len(jobs),
                    )
                    break
                new = 0
                for j in page_jobs:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id or "")
                    jobs.append(j)
                    new += 1
                consecutive_empty = 0 if new else consecutive_empty + 1
                page += 1
        return jobs

    # --- listing pages ------------------------------------------------------

    async def _fetch_listing_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> list[Job]:
        url = f"{self._base_url}/jobs?page={page}"
        text = await self._request_html(client, sem, url)
        return self._parse_listing(text)

    def _parse_listing(self, text: str) -> list[Job]:
        jobs: list[Job] = []
        for match in _CARD_RE.finditer(text):
            ats_id = match.group("id")
            body = match.group("body")
            job = self._parse_card(ats_id, body)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_card(self, ats_id: str, body: str) -> Job | None:
        anchor_m = _TITLE_ANCHOR_RE.search(body)
        if not anchor_m:
            return None
        anchor = anchor_m.group(0)
        href_m = _HREF_ATTR_RE.search(anchor)
        title_m = _TITLE_ATTR_RE.search(anchor)
        if not href_m or not title_m:
            return None
        url = html.unescape(href_m.group("url"))
        title = html.unescape(title_m.group("title")).strip()
        if not title:
            return None

        company_m = _COMPANY_RE.search(body)
        company = (
            _clean_text(company_m.group(1))
            if company_m else "Unknown"
        )

        # Badge order on every ROAM card is: location, employment type,
        # salary chip. We collect every ``bg-brand-secondary-100`` span
        # and triage by content.
        badges = [_clean_text(b) for b in _BADGE_RE.findall(body)]
        location: str | None = None
        employment_type: str | None = None
        commitment: str | None = None
        for chip in badges:
            normalized = chip.lower()
            if normalized in _EMPLOYMENT_MAP and not employment_type:
                employment_type = _EMPLOYMENT_MAP[normalized]
                commitment = chip
            elif _SALARY_RE.search(chip):
                # Salary chip is parsed separately below — skip it as a
                # location candidate.
                continue
            elif location is None and chip:
                location = chip

        salary_summary: str | None = None
        salary_min: float | None = None
        salary_max: float | None = None
        salary_currency: str | None = None
        sal_m = _SALARY_RE.search(body)
        if sal_m:
            salary_currency = sal_m.group("curr")
            salary_summary = (
                f"{salary_currency} {_clean_text(sal_m.group('body'))}"
            )
            salary_min, salary_max = _parse_salary_range(
                sal_m.group("body")
            )

        # "Posted N days ago" — listing card uses a plain "<N> days ago"
        # without a "Posted" prefix; either spelling is accepted.
        posted_at: datetime | None = None
        posted_text: str | None = None
        time_m = _TIME_AGO_RE.search(body)
        if time_m:
            posted_text = time_m.group(0)
            posted_at = _relative_to_datetime(
                int(time_m.group(1)), time_m.group(2).lower()
            )

        raw: dict[str, Any] = {
            "region_key": self.company_slug,
        }
        if posted_text:
            raw["posted_text"] = posted_text

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.ROAMAFRICA,
            ats_id=ats_id,
            location=location,
            country_iso=self._country_iso,
            region="Africa",
            language=self._language,
            salary_currency=salary_currency,
            salary_period="MONTH" if salary_currency else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw,
        )

    # --- detail-page JSON-LD (opt-in, used by enrichment) -------------------

    @staticmethod
    def parse_detail_jsonld(html_text: str) -> dict[str, Any] | None:
        """Extract the ``JobPosting`` JSON-LD block from a detail page.

        Returns the raw dict (so callers can pick whatever fields they
        want), or ``None`` when the page has no ``JobPosting`` node.
        Exposed as a staticmethod for reuse by downstream enrichment
        without instantiating the scraper.
        """
        for match in _LD_RE.finditer(html_text):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            graph = (
                payload.get("@graph", [payload])
                if isinstance(payload, dict) else payload
            )
            if not isinstance(graph, list):
                graph = [graph]
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
        return None

    # --- HTTP ---------------------------------------------------------------

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": _USER_AGENT,
                            "Accept": "text/html,*/*",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"ROAM Africa fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # Past the last page → empty body so the pager treats
                # it as "no new jobs" and stops after two such replies.
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"ROAM Africa returned {response.status_code} for "
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
                f"ROAM Africa returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"ROAM Africa exhausted retries for {url}: {last_exc}"
        )


# --- helpers ----------------------------------------------------------------


def _clean_text(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _parse_salary_range(body: str) -> tuple[float | None, float | None]:
    """Pull ``min - max`` out of a ROAM salary chip body.

    ROAM chips look like ``70,000 - 150,000`` (with thousand separators
    and an em / hyphen dash). Commission-only / negotiable strings
    have no numbers and return ``(None, None)``.
    """
    numbers = re.findall(r"[\d,]+(?:\.\d+)?", body)
    if not numbers:
        return None, None
    try:
        parsed = [float(n.replace(",", "")) for n in numbers]
    except ValueError:
        return None, None
    parsed = [p for p in parsed if p > 0]
    if not parsed:
        return None, None
    if len(parsed) == 1:
        return parsed[0], parsed[0]
    return parsed[0], parsed[-1]


def _relative_to_datetime(n: int, unit: str) -> datetime:
    """Map ``N hours/days/weeks/months/years ago`` → datetime.now() - delta.

    Approximate by design — ROAM doesn't expose an absolute datetime on
    the listing card, only on the detail page's JSON-LD. ``raw.posted_text``
    keeps the source string so downstream consumers can re-derive.
    """
    now = datetime.now()
    if unit == "hour":
        return now - timedelta(hours=n)
    if unit == "day":
        return now - timedelta(days=n)
    if unit == "week":
        return now - timedelta(weeks=n)
    if unit == "month":
        return now - timedelta(days=30 * n)
    if unit == "year":
        return now - timedelta(days=365 * n)
    return now
