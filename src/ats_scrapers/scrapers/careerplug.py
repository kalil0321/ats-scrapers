"""CareerPlug hiring sites (``{account}.careerplug.com``).

An account lists its open jobs on paginated HTML pages. The first page of a
single employer's site also embeds its postings as schema.org data,
description included. Later pages and the sites that gather the jobs of many
locations do not, so those descriptions come from the posting pages. A
posting page without that data still shows the posting's text.
``include_descriptions=False`` skips the posting pages and keeps what the
list embeds.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._posting_detail import body_text, fill_missing, job_posting
from ats_scrapers.scrapers._posting_fields import finite_number
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.scrapers._htmltree import Node

LIST_URL = "https://{slug}.careerplug.com/jobs"
JOB_URL = "https://{slug}.careerplug.com/jobs/{job_id}"
PAGE_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
MAX_PAGES = 2_000

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_JOB_PATH = re.compile(r"^/jobs/(\d+)$")
_PAGE_LINK = re.compile(r"[?&]page=(\d+)")
# The list prints a place as ``TX-Houston-77070``.
_PLACE = re.compile(
    r"^(?P<region>[A-Za-z]{2})-(?P<city>.+?)"
    r"(?:-(?P<postal>\d{5}(?:-\d{4})?|[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d))?$"
)
_POST_DATE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2})$")
_SITE_SUFFIX = re.compile(r"\s+Careers$", re.IGNORECASE)
_US_REGIONS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
    "GU", "VI",
})
_CA_REGIONS = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})
_EMPLOYMENT: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
}
_PERIODS: dict[str, SalaryPeriod] = {
    "HOUR": "HOUR", "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH", "YEAR": "YEAR",
}


@ScraperRegistry.register(ATSType.CAREERPLUG)
class CareerPlugScraper(BaseScraper):
    """Scrape one public CareerPlug hiring site."""

    ats = ATSType.CAREERPLUG

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
            company_slug, provider="CareerPlugScraper"
        ).lower()
        if self.company_slug in {"app", "www"}:
            raise ScraperError("CareerPlug requires an account's own hiring site")

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher(follow_redirects=False) as fetch:
            first = await self._page(fetch, 1)
            pages = [first]
            if first.last_page > MAX_PAGES:
                raise self._too_long()
            if first.last_page > 1:
                semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)
                pages += await asyncio.gather(*(
                    self._page(fetch, number, semaphore)
                    for number in range(2, first.last_page + 1)
                ))
            # The pager names the last page. Should a site not, keep turning.
            while pages[-1].has_next:
                if len(pages) >= MAX_PAGES:
                    raise self._too_long()
                pages.append(await self._page(fetch, len(pages) + 1))
        listed: dict[str, Job] = {}
        for page in pages:
            for job in page.jobs:
                listed.setdefault(job.ats_id, job)
        jobs = list(listed.values())
        if self.include_descriptions:
            await self._describe(jobs)
        return jobs

    def _too_long(self) -> ScraperError:
        # A list cut short would pass for a complete one.
        return ScraperError(
            f"CareerPlug ({self.company_slug}) lists more than {MAX_PAGES} pages of jobs"
        )

    def get_description(self, job: Job) -> str | None:
        if not job.description:
            self._run_sync(self._describe([job]))
        return job.description

    async def _page(
        self, fetch: Fetcher, number: int, semaphore: asyncio.Semaphore | None = None,
    ) -> _Page:
        url = LIST_URL.format(slug=self.company_slug)
        params = {"page": number} if number > 1 else None
        if semaphore is None:
            response = await fetch.request("GET", url, params=params, handled=_REDIRECTS)
        else:
            async with semaphore:
                response = await fetch.request(
                    "GET", url, params=params, handled=_REDIRECTS,
                )
        if response.status_code in _REDIRECTS:
            # An unknown account is sent to the platform's own front page.
            raise CompanyNotFoundError(
                f"CareerPlug ({self.company_slug}) has no hiring site at this address"
            )
        return self._parse_page(response.text)

    def _parse_page(self, page_html: str) -> _Page:
        tree = parse_html(page_html)
        marked = {node.attrs.get("id"): node for node in tree.walk() if "id" in node.attrs}
        table = marked.get("job_table")
        if table is None:
            # A site with nothing open shows its search form and no list.
            if "job_filters" in marked:
                return _Page(jobs=[], last_page=1, has_next=False)
            raise ScraperError(
                f"CareerPlug ({self.company_slug}) returned an unrecognized jobs page"
            )
        embedded = _embedded_postings(tree)
        site = _site_name(tree) or self.company_slug
        jobs: list[Job] = []
        rows = _rows(table)
        for link in table.walk():
            match = _JOB_PATH.match(link.attrs.get("href", "")) if link.tag == "a" else None
            if not match:
                continue
            job_id = match.group(1)
            title = _text(_by_class(link, "name"))
            if not title:
                continue
            posting = embedded.get(job_id, {})
            # The link wraps the whole row on most sites; on others it holds
            # only the title and the cells sit beside it.
            row = link if _by_class(link, "job-location") else rows.get(id(link), link)
            place = _listed_place(_cell(row, "job-location"))
            address = _address(posting)
            region = address.get("region") or place.get("region")
            city = address.get("city") or place.get("city")
            url = JOB_URL.format(slug=self.company_slug, job_id=job_id)
            jobs.append(Job(
                url=url,
                title=title,
                company=_organization(posting) or site,
                ats_type=self.ats,
                ats_id=job_id,
                location=", ".join(part for part in (city, region) if part) or place.get("text"),
                country_iso=address.get("country") or _country(region),
                apply_url=url,
                employment_type=_EMPLOYMENT.get(str(posting.get("employmentType") or "")),
                description=body_text(posting.get("description"))[:25_000] or None,
                posted_at=(
                    _iso_datetime(posting.get("datePosted"))
                    or _listed_date(_cell(row, "job-post-date"))
                ),
                fetched_at=datetime.now(UTC),
                raw=_raw(posting, address.get("postal") or place.get("postal")),
                **_salary(posting.get("baseSalary")),
            ))
        last_page = 1
        has_next = False
        pager = _by_class(tree, "pagination")
        if pager is not None:
            for anchor in pager.walk():
                if anchor.tag != "a":
                    continue
                found = _PAGE_LINK.search(anchor.attrs.get("href", ""))
                if found:
                    last_page = max(last_page, int(found.group(1)))
                has_next = has_next or "next_page" in anchor.classes
        return _Page(jobs=jobs, last_page=last_page, has_next=has_next)

    async def _describe(self, jobs: list[Job]) -> None:
        if all(job.description for job in jobs):
            return
        # The posting address forwards to the page that carries the posting.
        async with self.make_fetcher() as fetch:
            await fill_missing(jobs, lambda job: self._detail(fetch, job), DETAIL_CONCURRENCY)

    async def _detail(self, fetch: Fetcher, job: Job) -> None:
        response = await fetch.request("GET", str(job.url), handled=frozenset({404}))
        if response.status_code != 200:
            return
        posting = job_posting(response.text)
        if _posting_id(posting) != job.ats_id:
            # Some postings carry no schema.org data; the page still shows
            # their text.
            job.description = _page_description(response.text, job.ats_id)
            return
        job.description = body_text(posting.get("description"))[:25_000] or None
        job.company = _organization(posting) or job.company
        address = _address(posting)
        job.country_iso = address.get("country") or job.country_iso
        job.employment_type = job.employment_type or _EMPLOYMENT.get(
            str(posting.get("employmentType") or "")
        )
        job.posted_at = _iso_datetime(posting.get("datePosted")) or job.posted_at
        if job.salary_min is None and job.salary_max is None:
            for field, value in _salary(posting.get("baseSalary")).items():
                setattr(job, field, value)
        raw = {**(job.raw or {}), **(_raw(posting, address.get("postal")) or {})}
        job.raw = raw or None


class _Page:
    """One page of an account's job list."""

    __slots__ = ("has_next", "jobs", "last_page")

    def __init__(self, *, jobs: list[Job], last_page: int, has_next: bool) -> None:
        self.jobs = jobs
        self.last_page = last_page
        self.has_next = has_next


def _by_class(node: Node, name: str) -> Node | None:
    return next((child for child in node.walk() if name in child.classes), None)


def _text(node: Node | None) -> str:
    return node.text() if node is not None else ""


def _rows(table: Node) -> dict[int, Node]:
    """The innermost list row around each job link, keyed by ``id`` of the
    link."""
    rows: dict[int, Node] = {}
    for row in table.walk():
        if "row" not in row.classes:
            continue
        for node in row.walk():
            if node.tag == "a" and _JOB_PATH.match(node.attrs.get("href", "")):
                rows[id(node)] = row
    return rows


def _cell(row: Node, name: str) -> str:
    """A list cell's value, without the label shown beside it on small screens."""
    cell = _by_class(row, name)
    if cell is None:
        return ""
    label = _text(_by_class(cell, "job-row-title"))
    return cell.text().removeprefix(label).strip()


def _page_description(page: str, job_id: str) -> str | None:
    tree = parse_html(page)
    # The application form, and on some sites the canonical link, name the
    # posting the page belongs to.
    own = f"/jobs/{job_id}/apps"
    if not any(
        (node.tag == "form" and node.attrs.get("action", "") == own)
        or (node.tag == "link" and node.attrs.get("href", "").endswith(f"{own}/new"))
        for node in tree.walk()
    ):
        return None
    # The older page layout names the container by id.
    container = next(
        (
            node for node in tree.walk()
            if "job-description-container" in node.classes
            or node.attrs.get("id") == "job-description-wrapper"
        ),
        None,
    )
    # The container also holds the account's blurb and disclaimer, shared
    # by every posting.
    body = _by_class(container, "trix-content") if container is not None else None
    return _text_lines(body)[:25_000] or None


def _text_lines(node: Node | None) -> str:
    return node.lines() if node is not None else ""


def _embedded_postings(tree: Node) -> dict[str, dict[str, Any]]:
    postings: dict[str, dict[str, Any]] = {}
    for script in tree.walk():
        if script.tag != "script" or script.attrs.get("type") != "application/ld+json":
            continue
        try:
            block = json.loads("".join(part for part in script.children if isinstance(part, str)))
        except ValueError:
            continue
        elements = block.get("itemListElement") if isinstance(block, dict) else None
        if not isinstance(elements, list):
            continue
        for element in elements:
            item = element.get("item") if isinstance(element, dict) else None
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                job_id = _posting_id(item)
                if job_id:
                    postings.setdefault(job_id, item)
    return postings


def _posting_id(posting: dict[str, Any]) -> str | None:
    identifier = posting.get("identifier")
    value = identifier.get("value") if isinstance(identifier, dict) else None
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    return text or None


def _organization(posting: dict[str, Any]) -> str | None:
    organization = posting.get("hiringOrganization")
    name = organization.get("name") if isinstance(organization, dict) else None
    return " ".join(name.split()) or None if isinstance(name, str) else None


def _address(posting: dict[str, Any]) -> dict[str, str]:
    place = posting.get("jobLocation")
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return {}
    found: dict[str, str] = {}
    for key, field in (
        ("city", "addressLocality"), ("region", "addressRegion"),
        ("postal", "postalCode"), ("country", "addressCountry"),
    ):
        value = address.get(field)
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if text:
            found[key] = text
    country = found.pop("country", "")
    if len(country) == 2 and country.isalpha():
        found["country"] = country.upper()
    return found


def _salary(base: object) -> dict[str, Any]:
    value = base.get("value") if isinstance(base, dict) else None
    if not isinstance(base, dict) or not isinstance(value, dict):
        return {}
    minimum = _amount(value.get("minValue"))
    maximum = _amount(value.get("maxValue"))
    if minimum is None and maximum is None:
        minimum = maximum = _amount(value.get("value"))
    if minimum is None and maximum is None:
        return {}
    currency = base.get("currency")
    period = _PERIODS.get(str(value.get("unitText") or "").upper())
    if minimum is not None and maximum is not None and minimum != maximum:
        summary = f"{minimum:g} - {maximum:g}"
    else:
        summary = f"{minimum if minimum is not None else maximum:g}"
    if period:
        summary = f"{summary} per {period.casefold()}"
    return {
        "salary_currency": (
            currency.upper()
            if isinstance(currency, str) and len(currency) == 3 and currency.isalpha()
            else None
        ),
        "salary_period": period,
        "salary_summary": summary,
        "salary_min": minimum,
        "salary_max": maximum,
    }


def _amount(value: object) -> float | None:
    amount = finite_number(value)
    return amount if amount is not None and amount > 0 else None


def _raw(posting: dict[str, Any], postal: str | None) -> dict[str, Any] | None:
    industry = posting.get("industry")
    raw = {
        "industry": industry.strip() if isinstance(industry, str) else None,
        "postal_code": postal,
    }
    return {key: value for key, value in raw.items() if value} or None


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _listed_date(text: str) -> datetime | None:
    match = _POST_DATE.match(text)
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return datetime(2000 + year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _listed_place(text: str) -> dict[str, str]:
    if not text:
        return {}
    match = _PLACE.match(text)
    if not match:
        return {"text": text}
    place = {"region": match.group("region").upper(), "city": match.group("city").strip()}
    if match.group("postal"):
        place["postal"] = match.group("postal")
    return place


def _country(region: str | None) -> str | None:
    if region in _US_REGIONS:
        return "US"
    if region in _CA_REGIONS:
        return "CA"
    return None


def _site_name(tree: Node) -> str | None:
    return _SITE_SUFFIX.sub("", _text(tree.find("title"))) or None
