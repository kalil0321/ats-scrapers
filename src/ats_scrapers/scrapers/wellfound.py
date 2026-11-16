"""Wellfound (formerly AngelList Talent): startup jobs.

Wellfound has no public job API, but its role pages are rendered on the
server with their data in ``__NEXT_DATA__``::

    GET /role/{role}?page={n}  -> 20 startups, each with up to three
                                  listings, full descriptions included

A startup with more openings in a role than its three highlighted ones
shows only those three, so the scraper reads a curated set of roles, every
page of each, and deduplicates the listings by id. Coverage is that of the
roles read, not of the whole site.

Listings that Wellfound imported from an ATS (``atsSource``) or posted
automatically (``autoPosted``) are skipped by default: they are copies of
postings the ATS scrapers read at the source. Pass
``include_ats_imports=True`` to keep them.

Cloudflare answers some User-Agents with 403 and may do the same for
datacenter addresses; pass ``proxy=`` when the scraper runs from a server.

``firecrawl_api_key`` routes the page requests through Firecrawl's scrape
API (paid) instead. The ``FIRECRAWL_API_KEY`` environment variable alone
does not enable it.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._throttle import Throttle
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

WELLFOUND_BASE = "https://wellfound.com"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"
MAX_CONCURRENCY = 4
MAX_PAGES_PER_ROLE = 200
# Cloudflare challenges an address that asks for about 100 pages a minute.
REQUEST_INTERVAL_S = 1.0
# A page past the last one redirects to the first.
_NO_PAGE = frozenset({301, 302, 303, 307, 308, 404})

# The roles Wellfound links from its own role pages.
DEFAULT_ROLE_SLUGS: tuple[str, ...] = (
    "software-engineer",
    "frontend-engineer",
    "backend-engineer",
    "full-stack-engineer",
    "mobile-engineer",
    "devops-engineer",
    "engineering-manager",
    "data-engineer",
    "data-scientist",
    "data-analyst",
    "machine-learning-engineer",
    "product-manager",
    "designer",
    "product-designer",
    "ux-designer",
    "ui-ux-designer",
    "graphic-designer",
    "marketing-manager",
    "growth-marketer",
    "sales-manager",
    "account-executive",
    "account-manager",
    "operations-manager",
    "finance-manager",
    "financial-analyst",
    "hr-manager",
)

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_JSON_LD_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
_RESULTS_PREFIX = "seoLandingPageJobSearchResults("
_EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "internship": "INTERN",
}
# "$120k – $160k", "₹10L – ₹25L", "$90k – $120k CAD", "Up to $150k".
_SALARY_RE = re.compile(
    r"^(?:(?P<upto>up to)\s+)?(?P<sym>[^\d\s]+)(?P<lo>\d[\d,]*(?:\.\d+)?)(?P<lo_unit>[kKL]?)"
    r"(?:\s*[–-]\s*(?P=sym)?(?P<hi>\d[\d,]*(?:\.\d+)?)(?P<hi_unit>[kKL]?))?"
    r"(?:\s+(?P<code>[A-Z]{3}))?$",
    re.IGNORECASE,
)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "د.إ": "AED"}
_UNITS = {"": 1, "k": 1_000, "l": 100_000}


@ScraperRegistry.register(ATSType.WELLFOUND)
class WellfoundScraper(BaseScraper):
    """Wellfound (wellfound.com): startup jobs, read from its role pages.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``role_slugs``: the roles to read (``/role/{slug}``).
    - ``max_pages``: pages read per role at most.
    - ``request_interval``: seconds between the starts of two requests.
    - ``include_ats_imports``: keep listings imported from an ATS.
    - ``firecrawl_api_key``: fetch the pages through Firecrawl.
    """

    ats = ATSType.WELLFOUND
    # Cloudflare 403s httpx's default User-Agent.
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 60.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        role_slugs: tuple[str, ...] | list[str] = DEFAULT_ROLE_SLUGS,
        max_pages: int = MAX_PAGES_PER_ROLE,
        include_ats_imports: bool = False,
        request_interval: float = REQUEST_INTERVAL_S,
        firecrawl_api_key: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.role_slugs = tuple(role_slugs)
        self.max_pages = max_pages
        self.include_ats_imports = include_ats_imports
        self.request_interval = request_interval
        self.firecrawl_api_key = firecrawl_api_key or None

    async def afetch(self) -> list[Job]:
        if not self.role_slugs:
            return []
        throttle = Throttle(MAX_CONCURRENCY, self.request_interval)
        async with self.make_fetcher(follow_redirects=False) as fetch:
            per_role = await asyncio.gather(
                *(self._read_role(fetch, throttle, role) for role in self.role_slugs)
            )
        if all(listings is None for listings in per_role):
            raise ScraperError(
                f"Wellfound: none of the role pages exist ({', '.join(self.role_slugs)})"
            )
        jobs: dict[str, Job] = {}
        for listings in per_role:
            for startup, listing in listings or []:
                job = self._parse_listing(startup, listing)
                if job is not None and job.ats_id not in jobs:
                    jobs[job.ats_id] = job
        return list(jobs.values())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                page = await self._get_page(fetch, str(job.url))
            return _posting_description(page) if page else None

        return self._run_sync(run())

    async def _read_role(
        self, fetch: Fetcher, throttle: Throttle, role: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
        """Every listing on every page of ``role``; ``None`` if the role is gone."""
        first = await self._read_role_page(fetch, throttle, role, 1)
        if first is None:
            logger.warning("Wellfound role %r has no page; skipping it", role)
            return None
        listings, page_count = first
        last = min(page_count, self.max_pages)
        if page_count > last:
            logger.warning(
                "Wellfound role %r has %d pages; reading the first %d",
                role, page_count, last,
            )
        rest = await asyncio.gather(
            *(self._read_role_page(fetch, throttle, role, n) for n in range(2, last + 1))
        )
        for page in rest:
            if page is not None:
                listings.extend(page[0])
        return listings

    async def _read_role_page(
        self, fetch: Fetcher, throttle: Throttle, role: str, page: int
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int] | None:
        url = f"{WELLFOUND_BASE}/role/{role}"
        if page > 1:
            url += f"?page={page}"
        async with throttle:
            html = await self._get_page(fetch, url)
        return _role_listings(html, role, page, url) if html is not None else None

    async def _get_page(self, fetch: Fetcher, url: str) -> str | None:
        """The page, or ``None`` when it does not exist or redirects away."""
        if not self.firecrawl_api_key:
            response = await fetch.request("GET", url, handled=_NO_PAGE)
            return None if response.status_code in _NO_PAGE else response.text
        response = await fetch.request(
            "POST",
            FIRECRAWL_SCRAPE_URL,
            json={"url": url, "formats": ["rawHtml"]},
            headers={
                "Authorization": f"Bearer {self.firecrawl_api_key}",
                "Accept": "application/json",
            },
            handled={401, 402, 403},
        )
        if response.status_code != 200:
            raise ScraperError(
                f"Firecrawl returned {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        metadata = data.get("metadata") if isinstance(data, dict) else None
        if isinstance(metadata, dict) and metadata.get("statusCode") == 404:
            return None
        html = data.get("rawHtml") if isinstance(data, dict) else None
        if not isinstance(html, str):
            raise ScraperError(f"Firecrawl returned no page for {url}")
        return html

    def _parse_listing(self, startup: dict[str, Any], item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        slug = str(item.get("slug") or "").strip()
        company = str(startup.get("name") or "").strip()
        if not ats_id.isdigit() or not title or not slug or not company:
            return None
        ats_source = item.get("atsSource") or None
        auto_posted = item.get("autoPosted") is True
        if not self.include_ats_imports and (ats_source or auto_posted):
            return None

        compensation = str(item.get("compensation") or "").strip() or None
        currency, salary_min, salary_max = _parse_salary(compensation)
        locations = _strings(item.get("locationNames"))
        remote_locations = _strings(item.get("acceptedRemoteLocationNames"))
        live_start = item.get("liveStartAt")
        description = item.get("description")
        experience = item.get("yearsExperienceMin")

        raw: dict[str, Any] = {
            "company_slug": startup.get("slug"),
            "company_id": startup.get("id"),
            "role": item.get("primaryRoleTitle"),
            "remote_locations": remote_locations or None,
            "years_experience_max": item.get("yearsExperienceMax"),
            "ats_source": ats_source,
            "auto_posted": auto_posted or None,
        }
        return Job(
            url=HttpUrl(f"{WELLFOUND_BASE}/jobs/{ats_id}-{slug}"),
            title=title,
            company=company,
            ats_type=ATSType.WELLFOUND,
            ats_id=ats_id,
            location=", ".join(locations or remote_locations) or None,
            is_remote=item["remote"] if isinstance(item.get("remote"), bool) else None,
            salary_summary=compensation,
            salary_currency=currency,
            salary_period="YEAR" if currency else None,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience if type(experience) is int else None,
            employment_type=_EMPLOYMENT_TYPES.get(str(item.get("jobType"))),
            description=(
                description.strip()[:25_000]
                if self.include_descriptions and isinstance(description, str)
                and description.strip()
                else None
            ),
            posted_at=(
                datetime.fromtimestamp(live_start, UTC)
                if type(live_start) is int and live_start > 0
                else None
            ),
            fetched_at=datetime.now(UTC),
            raw={key: value for key, value in raw.items() if value is not None} or None,
        )


def _role_listings(
    html: str, role: str, page: int, url: str
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int] | None:
    """The ``(startup, listing)`` pairs of one role page and the role's page count.

    ``None`` when the page is the role's first page served in place of a page
    past the last one.
    """
    match = _NEXT_DATA_RE.search(html)
    try:
        data = json.loads(match.group(1)) if match else None
        cache = data["props"]["pageProps"]["apolloState"]["data"]
        queries = cache["ROOT_QUERY"]["talent"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ScraperError(f"Wellfound page {url} carries no job data") from exc
    pages: dict[object, Any] = {}
    for key, value in queries.items():
        if not key.startswith(_RESULTS_PREFIX):
            continue
        try:
            args = json.loads(key[len(_RESULTS_PREFIX):-1])
        except ValueError:
            continue
        if isinstance(args, dict) and args.get("role") == role:
            pages[args.get("page")] = value
    results = pages.get(page)
    if results is None and page > 1 and 1 in pages:
        return None
    if not isinstance(results, dict) or not isinstance(results.get("startups"), list):
        raise ScraperError(f"Wellfound page {url} carries no job search results")
    page_count = results.get("pageCount")
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for startup in _refs(cache, results["startups"]):
        for listing in _refs(cache, startup.get("highlightedJobListings")):
            pairs.append((startup, listing))
    return pairs, page_count if type(page_count) is int and page_count > 0 else 1


def _refs(cache: dict[str, Any], refs: object) -> list[dict[str, Any]]:
    """The Apollo cache entries ``refs`` point to."""
    if not isinstance(refs, list):
        return []
    entries = (
        cache.get(ref.get("__ref")) for ref in refs if isinstance(ref, dict)
    )
    return [entry for entry in entries if isinstance(entry, dict)]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ))


def _parse_salary(
    compensation: str | None,
) -> tuple[str | None, float | None, float | None]:
    """Currency and bounds of a yearly pay range; equity after "•" is ignored."""
    if not compensation:
        return None, None, None
    match = _SALARY_RE.match(compensation.split("•")[0].strip())
    if not match:
        return None, None, None
    currency = match["code"] or _CURRENCY_SYMBOLS.get(match["sym"])
    if currency is None:
        return None, None, None
    low = _amount(match["lo"], match["lo_unit"])
    if match["upto"]:
        return currency, None, low
    high = _amount(match["hi"], match["hi_unit"]) if match["hi"] else low
    return currency, low, high


def _amount(number: str, unit: str) -> float:
    return float(number.replace(",", "")) * _UNITS[unit.lower()]


def _posting_description(html: str) -> str | None:
    """The description of a job page's JobPosting JSON-LD, as text."""
    for match in _JSON_LD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            description = data.get("description")
            if isinstance(description, str) and description.strip():
                return parse_html(description).lines()[:25_000] or None
    return None
