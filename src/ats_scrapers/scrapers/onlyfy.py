"""onlyfy career pages (``{company}.onlyfy.jobs``).

A company's page lists its open jobs ten at a time. A job page carries the
posting as schema.org data, description included.
``include_descriptions=False`` reads the list alone.

A list is returned whole or not at all: a refused or failed page fails the
read, and so does a page that comes back empty before the end of the list.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, EngagementType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._posting_detail import body_text, fill_missing, job_posting
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable

    from ats_scrapers.fetch import Fetcher, FetchResponse
    from ats_scrapers.scrapers._htmltree import Node

SITE_URL = "https://{slug}.onlyfy.jobs"
PAGE_SIZE = 10
PAGE_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
MAX_PAGES = 1_000

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_MISSING = frozenset({404})
# Past the first page a job link carries the page it was listed on.
_JOB_PATH = re.compile(r"^/[a-z]{2}(?:-[A-Za-z]{2})?/job/([A-Za-z0-9]+)(?:\?.*)?$")
_LOCALE = re.compile(r"^/[a-z]{2}(?:-[A-Za-z]{2})?$")
_LISTED_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_TOTAL = re.compile(r"(\d+)")
_POSTED_EMPLOYMENT: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
}
# What a card calls the kind of work, in the two languages a page is printed in.
_LISTED: tuple[tuple[tuple[str, ...], EmploymentType | None, EngagementType | None], ...] = (
    (("apprenticeship", "ausbildung"), None, "apprenticeship"),
    (("internship", "praktik", "working student", "werkstudent"), "INTERN", "internship"),
    (("freelance", "freiberuf"), "CONTRACT", "independent_contractor"),
    (("temporary", "befristet"), "TEMPORARY", "temporary"),
    (("part-time", "teilzeit"), "PART_TIME", None),
    (("full-time", "vollzeit"), "FULL_TIME", None),
)


@ScraperRegistry.register(ATSType.ONLYFY)
class OnlyfyScraper(BaseScraper):
    """Scrape one company's public onlyfy career page."""

    ats = ATSType.ONLYFY

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }

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
        self.company_slug = require_host_label(
            company_slug, provider="OnlyfyScraper"
        ).lower()
        self._site = SITE_URL.format(slug=self.company_slug)
        self._company = self.company_slug

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher(follow_redirects=False) as fetch:
            locale, first = await self._first_page(fetch)
            pages = [first]
            last_page = math.ceil(first.total / PAGE_SIZE)
            if last_page > MAX_PAGES:
                # A list cut short would pass for a complete one.
                raise ScraperError(
                    f"onlyfy ({self.company_slug}) lists more than {MAX_PAGES} pages of jobs"
                )
            if last_page > 1:
                semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)
                pages += await _together(
                    self._page(fetch, locale, number, semaphore)
                    for number in range(2, last_page + 1)
                )
                # Only the last page may have emptied while the list was read.
                for number, page in enumerate(pages[:-1], start=1):
                    if not page.jobs:
                        raise ScraperError(
                            f"onlyfy ({self.company_slug}) list broke off at "
                            f"page {number} of {last_page}"
                        )
        listed: dict[str, Job] = {}
        for page in pages:
            for job in page.jobs:
                listed.setdefault(job.ats_id, job)
        jobs = list(listed.values())
        if self.include_descriptions:
            await self._describe(jobs)
        return jobs

    def get_description(self, job: Job) -> str | None:
        if not job.description:
            self._run_sync(self._describe([job]))
        return job.description

    async def _first_page(self, fetch: Fetcher) -> tuple[str, _Page]:
        """Read the English page, or the one language the company publishes."""
        locale = "/en"
        response = await fetch.request("GET", self._site + locale, handled=_REDIRECTS)
        if response.status_code in _REDIRECTS:
            locale = self._same_site_path(response)
            response = await fetch.request("GET", self._site + locale, handled=_REDIRECTS)
            if response.status_code in _REDIRECTS:
                raise self._astray(response)
        tree = parse_html(response.text)
        if _marked(tree, "jobs-results-announcement") is None:
            raise ScraperError(
                f"onlyfy ({self.company_slug}) returned an unrecognized career page"
            )
        title = tree.find("title")
        self._company = " ".join(title.text().split()) if title is not None else ""
        self._company = self._company or self.company_slug
        jobs = self._jobs(tree, locale)
        count = _marked(tree, "pagination-items-count")
        total = len(jobs)
        if count is not None:
            # "1-10 out of 20 jobs": the last number is the whole list.
            numbers = _TOTAL.findall(count.text())
            total = max(total, int(numbers[-1])) if numbers else total
        if total and not jobs:
            raise ScraperError(
                f"onlyfy ({self.company_slug}) announces {total} jobs and lists none"
            )
        return locale, _Page(jobs=jobs, total=total)

    def _same_site_path(self, response: FetchResponse) -> str:
        """The language a company's page moved to. A move to another site is no page."""
        target = urlsplit(response.headers.get("location", ""))
        if target.netloc and target.netloc.lower() != urlsplit(self._site).netloc:
            raise CompanyNotFoundError(
                f"onlyfy career page moved to {target.netloc}: {self.company_slug}"
            )
        if not _LOCALE.match(target.path) or target.path == "/en":
            raise self._astray(response)
        return target.path

    def _astray(self, response: FetchResponse) -> ScraperError:
        # A closed page answers 404 or moves to the company's own site. Any other
        # move is not a closed page, and must not pass for one.
        target = response.headers.get("location", "")[:200]
        return ScraperError(
            f"onlyfy ({self.company_slug}) sent the reader to an unexpected page: {target!r}"
        )

    async def _page(
        self, fetch: Fetcher, locale: str, number: int, semaphore: asyncio.Semaphore,
    ) -> _Page:
        async with semaphore:
            response = await fetch.request(
                "GET", self._site + locale, params={"page": number},
                handled=_REDIRECTS | _MISSING,
            )
        if response.status_code == 404:
            # The company answered a moment ago: its list is broken, not closed.
            raise ScraperError(
                f"onlyfy ({self.company_slug}) page {number} of the list returned 404"
            )
        if response.status_code in _REDIRECTS:
            target = urlsplit(response.headers.get("location", ""))
            same_site = not target.netloc or target.netloc.lower() == urlsplit(self._site).netloc
            if not same_site or target.path != locale:
                raise self._astray(response)
            # A page past the end sends the reader back to the first one.
            return _Page(jobs=[], total=0)
        tree = parse_html(response.text)
        if _marked(tree, "jobs-results-announcement") is None:
            raise ScraperError(
                f"onlyfy ({self.company_slug}) returned an unrecognized page {number} of the list"
            )
        return _Page(jobs=self._jobs(tree, locale), total=0)

    def _jobs(self, tree: Node, locale: str) -> list[Job]:
        jobs: list[Job] = []
        for card in tree.walk():
            if card.tag != "a" or card.attrs.get("data-testid") != "job-card":
                continue
            match = _JOB_PATH.match(card.attrs.get("href", ""))
            heading = _marked(card, "job-title")
            title = heading.text() if heading is not None else card.attrs.get("aria-label", "")
            title = " ".join(title.split())
            if not match or not title:
                continue
            info = _marked(card, "job-more-info")
            facts = _facts(info.text() if info is not None else "")
            url = f"{self._site}{locale}/job/{match.group(1)}"
            jobs.append(Job(
                url=url,
                title=title,
                company=self._company,
                ats_type=self.ats,
                ats_id=match.group(1),
                apply_url=url,
                fetched_at=datetime.now(UTC),
                **facts,
            ))
        return jobs

    async def _describe(self, jobs: list[Job]) -> None:
        if all(job.description for job in jobs):
            return
        async with self.make_fetcher(follow_redirects=False) as fetch:
            await fill_missing(jobs, lambda job: self._detail(fetch, job), DETAIL_CONCURRENCY)

    async def _detail(self, fetch: Fetcher, job: Job) -> None:
        response = await fetch.request(
            "GET", str(job.url), handled=_REDIRECTS | _MISSING,
        )
        posting = job_posting(response.text) if response.status_code == 200 else {}
        if not posting:
            return
        job.description = body_text(posting.get("description"))[:25_000] or None
        organization = posting.get("hiringOrganization")
        name = organization.get("name") if isinstance(organization, dict) else None
        if isinstance(name, str) and name.strip():
            job.company = " ".join(name.split())
        address = _address(posting)
        job.country_iso = address.get("country") or job.country_iso
        if not job.location:
            job.location = address.get("city")
        job.employment_type = job.employment_type or _POSTED_EMPLOYMENT.get(
            str(posting.get("employmentType") or "").upper()
        )
        job.posted_at = job.posted_at or _posted_date(posting.get("datePosted"))
        if address.get("postal_code"):
            job.raw = {**(job.raw or {}), "postal_code": address["postal_code"]}


class _Page:
    """One slice of a company's job list."""

    __slots__ = ("jobs", "total")

    def __init__(self, *, jobs: list[Job], total: int) -> None:
        self.jobs = jobs
        self.total = total


async def _together(reads: Iterable[Coroutine[Any, Any, _Page]]) -> list[_Page]:
    """Read pages side by side. One failure stops the others before the client closes."""
    tasks = [asyncio.ensure_future(read) for read in reads]
    try:
        return list(await asyncio.gather(*tasks))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _marked(tree: Node, name: str) -> Node | None:
    return next(
        (node for node in tree.walk() if node.attrs.get("data-testid") == name), None,
    )


def _facts(text: str) -> dict[str, Any]:
    """A card prints place, kind of work and date, then fields of the company's own."""
    parts = [part.strip() for part in text.split(" | ") if part.strip()]
    facts: dict[str, Any] = {}
    for index, part in enumerate(parts[:3]):
        date = _LISTED_DATE.match(part)
        if date:
            # 31.02. is printed now and then: such a day is left out.
            with contextlib.suppress(ValueError):
                facts["posted_at"] = datetime(
                    int(date.group(3)), int(date.group(2)), int(date.group(1)), tzinfo=UTC,
                )
            continue
        label = part.casefold()
        kind = next((found for found in _LISTED if any(word in label for word in found[0])), None)
        if kind and "commitment" not in facts:
            facts["commitment"] = part
            facts["employment_type"] = kind[1]
            facts["engagement_type"] = kind[2]
        elif index == 0:
            facts["location"] = part
    return facts


def _address(posting: dict[str, Any]) -> dict[str, str]:
    place = posting.get("jobLocation")
    if isinstance(place, list):
        place = place[0] if place else None
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return {}
    found: dict[str, str] = {}
    for key, field in (
        ("city", "addressLocality"), ("country", "addressCountry"), ("postal_code", "postalCode"),
    ):
        value = address.get(field)
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if text:
            found[key] = text
    country = found.pop("country", "")
    if len(country) == 2 and country.isalpha():
        found["country"] = country.upper()
    return found


def _posted_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
