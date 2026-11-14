"""Job boards that publish a listings sitemap and one page per job.

Jobberman and BrighterMonday run one country site each on the same platform.
A site lists its open jobs in ``/sitemap-listings-index-en.xml`` and each job
page carries a schema.org ``JobPosting`` record. Search pages are not read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import EmploymentType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers.base import BaseScraper

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

SITEMAP_INDEX = "/sitemap-listings-index-en.xml"
DETAIL_CONCURRENCY = 2
# Jobs close while the site is read; more unreadable pages than this share
# means the site changed or refused the read.
MAX_UNREADABLE_SHARE = 0.1

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_JSON_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
_LISTING_ID_RE = re.compile(r"listing-(\d+)$")
_EMPLOYMENT: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
}
_SALARY_PERIODS = frozenset({"HOUR", "DAY", "WEEK", "MONTH", "YEAR"})


class ListingsBoardScraper(BaseScraper):
    """The country sites of one board.

    ``company_slug`` is a country code (``ng``) or a site address for one
    site; anything else, such as ``all``, reads every site.
    """

    #: Country code -> (host, country name).
    sites: ClassVar[dict[str, tuple[str, str]]]

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        code = self._site_code(company_slug)
        super().__init__(
            code or "all",
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.codes = [code] if code else list(self.sites)

    @classmethod
    def _site_code(cls, company_slug: str) -> str | None:
        slug = company_slug.strip().lower()
        if slug in cls.sites:
            return slug
        host = urlparse(slug if "://" in slug else f"https://{slug}").hostname or ""
        for code, (site_host, _) in cls.sites.items():
            if host in (site_host, site_host.removeprefix("www.")):
                return code
        return None

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        async with self.make_fetcher() as fetch:
            for code in self.codes:
                for job in await self._site_jobs(fetch, code):
                    if job.ats_id:
                        jobs.setdefault(job.ats_id, job)
        return list(jobs.values())

    async def _site_jobs(self, fetch: Fetcher, code: str) -> list[Job]:
        host = self.sites[code][0]
        urls = await _job_urls(fetch, host)
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
        jobs: list[Job] = []
        unreadable: list[str] = []

        async def read(url: str) -> None:
            async with semaphore:
                try:
                    page = await fetch.get_text(url)
                except CompanyNotFoundError:
                    return  # Closed since the sitemap was written.
                except ScraperError as exc:
                    logger.warning("%s could not be read: %s", url, exc)
                    unreadable.append(url)
                    return
            job = self._parse(page, url, code)
            if job is None:
                unreadable.append(url)
            else:
                jobs.append(job)

        await asyncio.gather(*(read(url) for url in urls))
        if len(unreadable) > len(urls) * MAX_UNREADABLE_SHARE:
            raise ScraperError(
                f"{host}: {len(unreadable)} of {len(urls)} job pages could not be read"
            )
        return jobs

    def _parse(self, page: str, url: str, code: str) -> Job | None:
        nodes = _nodes(page)
        posting = next(
            (node for node in nodes.values() if node.get("@type") == "JobPosting"), None
        )
        if posting is None:
            return None
        title = _text(posting.get("title"))
        company = _text(_linked(nodes, posting.get("hiringOrganization")).get("name"))
        if not title or not company:
            return None
        listing = _LISTING_ID_RE.search(str(posting.get("@id") or ""))
        key = listing.group(1) if listing else urlparse(url).path.rsplit("/", 1)[-1]
        place = _linked(nodes, posting.get("jobLocation"))
        address = {**_dict(place.get("address")), **_linked(nodes, place.get("address"))}
        country = self.sites[code][1]
        area = _text(address.get("streetAddress"))
        abroad = bool(area) and area.lower().startswith("outside ")
        if area and (
            area.lower() in (country.lower(), "unspecified")
            or area.lower().startswith("rest of ")
        ):
            area = None
        salary = _dict(posting.get("baseSalary"))
        amounts = _dict(salary.get("value"))
        low = _number(amounts.get("minValue")) or _number(amounts.get("value"))
        high = _number(amounts.get("maxValue")) or low
        currency = _text(salary.get("currency")) if low is not None else None
        period = str(amounts.get("unitText") or "").upper()
        months = _dict(posting.get("experienceRequirements")).get("monthsOfExperience")
        html = posting.get("description")
        description = None
        if self.include_descriptions and isinstance(html, str):
            description = parse_html(html).lines().strip()[:25_000] or None
        raw = {
            key_: value
            for key_, value in (
                ("industry", _text(posting.get("industry"))),
                ("level", _text(posting.get("qualifications"))),
            )
            if value
        }
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=self.ats,
            ats_id=f"{code}-{key}",
            location=area if abroad else ", ".join(filter(None, (area, country))),
            country_iso=None if abroad else code.upper(),
            region=None if abroad else "Africa",
            is_remote=True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
            salary_currency=currency,
            salary_period=period if low is not None and period in _SALARY_PERIODS else None,
            salary_summary=_salary_summary(currency, low, high, period),
            salary_min=low,
            salary_max=high,
            experience=months // 12 if isinstance(months, int) and months >= 12 else None,
            employment_type=_EMPLOYMENT.get(str(posting.get("employmentType") or "")),
            department=_text(posting.get("occupationalCategory")),
            description=description,
            posted_at=_time(posting.get("datePosted")),
            application_deadline=_time(posting.get("validThrough")),
            raw=raw or None,
        )


async def _job_urls(fetch: Fetcher, host: str) -> list[str]:
    """The job pages a site lists in its listings sitemaps."""

    def ours(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == host

    index = await fetch.get_text(f"https://{host}{SITEMAP_INDEX}")
    sitemaps = [url for url in _LOC_RE.findall(index) if ours(url)]
    if not sitemaps:
        raise ScraperError(f"{host} lists no listings sitemap")
    urls: dict[str, None] = {}
    for sitemap in sitemaps:
        for url in _LOC_RE.findall(await fetch.get_text(sitemap)):
            if ours(url) and urlparse(url).path.startswith("/listings/"):
                urls.setdefault(url)
    return list(urls)


def _nodes(page: str) -> dict[str, dict[str, Any]]:
    """Every schema.org record of the page, by ``@id`` (or position)."""
    nodes: dict[str, dict[str, Any]] = {}
    for block in _JSON_LD_RE.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else data
        items = graph if isinstance(graph, list) else [data]
        for item in items:
            if isinstance(item, dict):
                nodes[str(item.get("@id") or f"#{len(nodes)}")] = item
    return nodes


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _linked(nodes: dict[str, dict[str, Any]], reference: object) -> dict[str, Any]:
    """The record a ``{"@id": ...}`` reference points to, or the value itself."""
    value = _dict(reference)
    return nodes.get(str(value.get("@id") or ""), value)


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _salary_summary(
    currency: str | None, low: float | None, high: float | None, period: str
) -> str | None:
    amounts = [f"{amount:,.0f}" for amount in (low, high) if amount is not None]
    if not currency or not amounts:
        return None
    span = " - ".join(dict.fromkeys(amounts))
    return f"{currency} {span} per {period.lower()}" if period else f"{currency} {span}"


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None
