"""Pandapé career pages (``{company}.pandape.infojobs.com.br``).

A company's vacancy page prints its first open vacancies and loads the rest
twenty at a time. A vacancy page carries the posting as schema.org data, description
included; a page that leaves the data out repeats its text in its description tag.
``include_descriptions=False`` reads the list alone.

A card prints the day and month of a vacancy, not its year. The list runs
newest first, so the year steps back each time a date would otherwise fall
after the one before it.

A list is returned whole or not at all: a refused or failed page fails the
read. A list that changes while its pages are read, or comes back shorter than
the count the site announces, is read once more. A long list rarely stands
still that long: the second reading may be a few vacancies short, one in five
hundred, and fails beyond that.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import (
    ATSType,
    EmploymentType,
    EngagementType,
    Job,
    SalaryPeriod,
    WorkplaceType,
)
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._posting_detail import body_text, fill_missing, job_posting
from ats_scrapers.scrapers._posting_fields import finite_number
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable

    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.scrapers._htmltree import Node

LIST_URL = "https://{slug}.pandape.infojobs.com.br/Vacancies"
PAGE_URL = "https://{slug}.pandape.infojobs.com.br/ListVacancies"
JOB_URL = "https://{slug}.pandape.infojobs.com.br/Detail/{job_id}"
PAGE_SIZE = 20
PAGE_CONCURRENCY = 4
DETAIL_CONCURRENCY = 4
MAX_PAGES = 2_000
MAX_LIST_ATTEMPTS = 2

# The site prints Brasília wall-clock time, which has kept one offset since 2019.
_SITE_TIME = timezone(timedelta(hours=-3))
_MISSING = frozenset({404})
_JOB_PATH = re.compile(r"^/Detail/(\d+)$")
_PLACE = re.compile(r"^(?P<city>.+?)\s+-\s+(?P<region>[A-Z]{2})$")
_LISTED_DATE = re.compile(r"^(\d{1,2})\s+([a-zç]{3})\.?$", re.IGNORECASE)
_AMOUNT = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)")
_OPENINGS = re.compile(r"^(\d+)\s+Posi")
_SITE_SUFFIX = re.compile(r"\s+-\s+Trabalhe Conosco\b.*$", re.IGNORECASE)
_UNNAMED = "empresa com nome confidencial"
_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_STATES = frozenset({
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG",
    "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE",
    "TO",
})
_WORKPLACES: dict[str, WorkplaceType] = {
    "presencial": "onsite",
    "híbrido": "hybrid",
    "remoto": "remote",
    "home office": "remote",
}
_POSTED_EMPLOYMENT: dict[str, EmploymentType] = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
}
_PERIODS: dict[str, SalaryPeriod] = {
    "HOUR": "HOUR", "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH", "YEAR": "YEAR",
}


class _ListChangedError(ScraperError):
    """The list changed, or came back short, while its pages were read."""


@ScraperRegistry.register(ATSType.PANDAPE)
class PandapeScraper(BaseScraper):
    """Scrape one company's public Pandapé career page."""

    ats = ATSType.PANDAPE

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/json",
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
            company_slug, provider="PandapeScraper"
        ).lower()
        if self.company_slug in {"www", "empregos"}:
            raise ScraperError("Pandapé requires a company's own career page")
        self._company = self.company_slug
        self._listed: dict[str, tuple[int, int]] = {}

    async def afetch(self) -> list[Job]:
        for attempt in range(1, MAX_LIST_ATTEMPTS + 1):
            try:
                jobs = await self._list(last_reading=attempt == MAX_LIST_ATTEMPTS)
                break
            except _ListChangedError:
                if attempt == MAX_LIST_ATTEMPTS:
                    raise
        _date_in_order(jobs, self._listed, datetime.now(_SITE_TIME).date())
        if self.include_descriptions:
            await self._describe(jobs)
        return jobs

    async def _list(self, *, last_reading: bool = False) -> list[Job]:
        # The days printed on the cards belong to one reading of the list.
        self._listed = {}
        async with self.make_fetcher() as fetch:
            response = await fetch.request("GET", LIST_URL.format(slug=self.company_slug))
            first = self._parse_list(response.text)
            pages = [first]
            if not first.is_last:
                second = await self._page(fetch, 2)
                pages.append(second)
                last_page = math.ceil(second.total / PAGE_SIZE) if second.total else 2
                if last_page > MAX_PAGES:
                    raise self._too_long()
                if last_page > 2:
                    semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)
                    pages += await _together(
                        self._page(fetch, number, semaphore)
                        for number in range(3, last_page + 1)
                    )
                # The count can grow while the list is read. Keep turning.
                while not pages[-1].is_last:
                    if len(pages) >= MAX_PAGES:
                        raise self._too_long()
                    pages.append(await self._page(fetch, len(pages) + 1))
        # Only the last page may have emptied while the list was read.
        for number, page in enumerate(pages[:-1], start=1):
            if not page.jobs:
                raise _ListChangedError(
                    f"Pandapé ({self.company_slug}) list broke off at "
                    f"page {number} of {len(pages)}"
                )
        listed: dict[str, Job] = {}
        for page in pages:
            for job in page.jobs:
                listed.setdefault(job.ats_id, job)
        # The count the last page that carries one announces: the list as it stands.
        announced = next((page.total for page in reversed(pages) if page.total), 0)
        if announced - len(listed) > (_short_by_at_most(announced) if last_reading else 0):
            # A vacancy that moved between two pages while they were read is on neither.
            raise _ListChangedError(
                f"Pandapé ({self.company_slug}) listed {len(listed)} of the "
                f"{announced} vacancies it announces"
            )
        return list(listed.values())

    def _too_long(self) -> ScraperError:
        # A list cut short would pass for a complete one.
        return ScraperError(
            f"Pandapé ({self.company_slug}) lists more than {MAX_PAGES} pages of vacancies"
        )

    def get_description(self, job: Job) -> str | None:
        if not job.description:
            self._run_sync(self._describe([job]))
        return job.description

    def _parse_list(self, page_html: str) -> _Page:
        tree = parse_html(page_html)
        marked = {node.attrs.get("id"): node for node in tree.walk() if "id" in node.attrs}
        if "hdn_PageNumber" not in marked:
            raise ScraperError(
                f"Pandapé ({self.company_slug}) returned an unrecognized career page"
            )
        self._company = _site_name(tree) or self.company_slug
        last = marked.get("hdn_isLast")
        is_last = last is None or last.attrs.get("value", "").lower() != "false"
        jobs = self._jobs(tree)
        if not jobs and not is_last:
            raise ScraperError(
                f"Pandapé ({self.company_slug}) page 1 lists nothing although more is announced"
            )
        return _Page(jobs=jobs, is_last=is_last, total=0)

    async def _page(
        self, fetch: Fetcher, number: int, semaphore: asyncio.Semaphore | None = None,
    ) -> _Page:
        url = PAGE_URL.format(slug=self.company_slug)
        params = {"PageNumber": number, "PageSize": PAGE_SIZE}
        if semaphore is None:
            response = await fetch.request("POST", url, params=params, handled=_MISSING)
        else:
            async with semaphore:
                response = await fetch.request("POST", url, params=params, handled=_MISSING)
        if response.status_code == 404:
            # The company answered a moment ago: its list is broken, not closed.
            raise ScraperError(
                f"Pandapé ({self.company_slug}) page {number} of the list returned 404"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ScraperError(
                f"Pandapé ({self.company_slug}) returned an unreadable page {number}"
            ) from error
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Pandapé ({self.company_slug}) returned an unreadable page {number}"
            )
        view = payload.get("view")
        jobs = self._jobs(parse_html(view)) if isinstance(view, str) else []
        is_last = payload.get("isLast") is not False
        if not jobs and not is_last:
            # More is announced and nothing is listed: a break, not the end.
            raise ScraperError(
                f"Pandapé ({self.company_slug}) page {number} lists nothing "
                "although more is announced"
            )
        total = payload.get("subtotal")
        return _Page(
            jobs=jobs,
            is_last=is_last,
            total=total if isinstance(total, int) and total > 0 else 0,
        )

    def _jobs(self, tree: Node) -> list[Job]:
        jobs: list[Job] = []
        for card in tree.walk():
            if card.tag != "a" or "card-vacancy" not in card.classes:
                continue
            match = _JOB_PATH.match(card.attrs.get("href", ""))
            heading = card.find("h3")
            title = (
                (heading.attrs.get("title") or heading.text()).strip()
                if heading is not None
                else ""
            )
            if not match or not title:
                continue
            job_id = match.group(1)
            printed = _listed_day(_date_text(card))
            if printed:
                self._listed.setdefault(job_id, printed)
            facts = _facts(card)
            place = _place(facts.get("location-pin", ""))
            contract = facts.get("sheet", "")
            schedule = facts.get("clock", "")
            url = JOB_URL.format(slug=self.company_slug, job_id=job_id)
            openings = _OPENINGS.match(facts.get("candidates", ""))
            raw = {
                "contract": contract or None,
                "openings": int(openings.group(1)) if openings else None,
            }
            jobs.append(Job(
                url=url,
                title=title,
                company=self._company,
                ats_type=self.ats,
                ats_id=job_id,
                location=place.get("text"),
                country_iso=place.get("country"),
                workplace_type=facts.get("workplace"),
                is_remote=True if facts.get("workplace") == "remote" else None,
                apply_url=url,
                employment_type=_employment(contract, schedule),
                engagement_type=_engagement(contract),
                commitment=schedule or None,
                fetched_at=datetime.now(UTC),
                raw={key: value for key, value in raw.items() if value} or None,
                **_listed_salary(facts.get("wallet", "")),
            ))
        return jobs

    async def _describe(self, jobs: list[Job]) -> None:
        if all(job.description for job in jobs):
            return
        async with self.make_fetcher() as fetch:
            await fill_missing(jobs, lambda job: self._detail(fetch, job), DETAIL_CONCURRENCY)

    async def _detail(self, fetch: Fetcher, job: Job) -> None:
        response = await fetch.request("GET", str(job.url), handled=_MISSING)
        if response.status_code != 200:
            return
        posting = job_posting(response.text)
        if not posting:
            if job.ats_id:
                job.description = _printed_text(response.text, job.ats_id)
            return
        job.description = body_text(posting.get("description"))[:25_000] or None
        organization = _organization(posting)
        if organization and organization.casefold() != _UNNAMED:
            job.company = organization
        address = _address(posting)
        job.country_iso = address.get("country") or job.country_iso
        if not job.location:
            job.location = ", ".join(
                part for part in (address.get("city"), address.get("region")) if part
            ) or None
        job.employment_type = job.employment_type or _POSTED_EMPLOYMENT.get(
            str(posting.get("employmentType") or "").casefold()
        )
        job.posted_at = _posted_datetime(posting.get("datePosted")) or job.posted_at
        for field, value in _posted_salary(posting.get("baseSalary")).items():
            if value is not None:
                setattr(job, field, value)


class _Page:
    """One slice of a company's vacancy list."""

    __slots__ = ("is_last", "jobs", "total")

    def __init__(self, *, jobs: list[Job], is_last: bool, total: int) -> None:
        self.jobs = jobs
        self.is_last = is_last
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


def _facts(card: Node) -> dict[str, Any]:
    """The labelled values of a vacancy card, keyed by the icon beside each."""
    facts: dict[str, Any] = {}
    for cell in card.walk():
        if cell.tag != "div" or "align-middle" not in cell.classes:
            continue
        if "icon-container" in cell.classes:
            continue
        icon = next(
            (
                name.removeprefix("icon-")
                for node in cell.walk()
                if node.tag == "i"
                for name in node.classes
                if name.startswith("icon-")
            ),
            None,
        )
        text = cell.text()
        if not icon or not text:
            continue
        workplace = _WORKPLACES.get(text.casefold())
        if workplace:
            facts["workplace"] = workplace
        else:
            # ``location-pin-1`` and its siblings name the same value.
            facts.setdefault(re.sub(r"-\d+$", "", icon), text)
    return facts


def _date_text(card: Node) -> str:
    node = next((child for child in card.walk() if "vacancy-date" in child.classes), None)
    return node.text() if node is not None else ""


def _place(text: str) -> dict[str, str]:
    if not text:
        return {}
    match = _PLACE.match(text)
    if not match:
        return {"text": text}
    region = match.group("region")
    place = {"text": f"{match.group('city')}, {region}"}
    if region in _STATES:
        place["country"] = "BR"
    return place


def _employment(contract: str, schedule: str) -> EmploymentType | None:
    label = contract.casefold()
    if "estágio" in label or "estagi" in label:
        return "INTERN"
    if "temporário" in label:
        return "TEMPORARY"
    if "prestador" in label or "autônomo" in label or "freelance" in label:
        return "CONTRACT"
    hours = schedule.casefold()
    if hours.startswith("período integral"):
        return "FULL_TIME"
    if hours.startswith(("parcial", "meio período")):
        return "PART_TIME"
    return None


def _engagement(contract: str) -> EngagementType | None:
    label = contract.casefold()
    if "estágio" in label or "estagi" in label:
        return "internship"
    if "aprendiz" in label:
        return "apprenticeship"
    if "temporário" in label:
        return "temporary"
    if "prestador" in label or "autônomo" in label or "freelance" in label:
        return "independent_contractor"
    if label.startswith("efetivo"):
        return "permanent"
    return None


def _listed_salary(text: str) -> dict[str, Any]:
    amounts = [
        float(found.replace(".", "").replace(",", "."))
        for found in _AMOUNT.findall(text)
    ]
    amounts = [amount for amount in amounts if amount > 0]
    if not amounts or len(amounts) > 2:
        return {}
    return {
        "salary_currency": "BRL",
        "salary_summary": text,
        "salary_min": min(amounts),
        "salary_max": max(amounts),
    }


def _posted_salary(base: object) -> dict[str, Any]:
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
        "salary_min": minimum if minimum is not None else maximum,
        "salary_max": maximum if maximum is not None else minimum,
    }


def _amount(value: object) -> float | None:
    amount = finite_number(value)
    return amount if amount is not None and amount > 0 else None


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
        ("country", "addressCountry"),
    ):
        value = address.get(field)
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if text:
            found[key] = text
    country = found.pop("country", "")
    if len(country) == 2 and country.isalpha():
        found["country"] = country.upper()
    return found


def _posted_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    # Seven fractional digits are printed; the standard library reads six.
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SITE_TIME)
    return parsed.astimezone(UTC)


def _listed_day(text: str) -> tuple[int, int] | None:
    match = _LISTED_DATE.match(text.strip())
    month = _MONTHS.get(match.group(2).lower()) if match else None
    return (month, int(match.group(1))) if match and month else None


def _date_in_order(jobs: list[Job], listed: dict[str, tuple[int, int]], today: date) -> None:
    """Give each job its listed day, with the year the order of the list implies."""
    newest = today + timedelta(days=1)
    previous: date | None = None
    for job in jobs:
        printed = listed.get(job.ats_id or "")
        if printed is None:
            continue
        month, day = printed
        # A neighbour may sit a few days out of order; a month's slack keeps its year.
        limit = newest if previous is None else min(newest, previous + timedelta(days=31))
        found: date | None = None
        for year in range(limit.year, limit.year - 12, -1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                # 29 February of a year without one.
                continue
            if candidate <= limit:
                found = candidate
                break
        if found is None:
            continue
        previous = found
        job.posted_at = datetime(
            found.year, found.month, found.day, tzinfo=_SITE_TIME,
        ).astimezone(UTC)


def _short_by_at_most(announced: int) -> int:
    """How many vacancies the last reading of a list may be short of.

    Each vacancy that closes while the pages are read moves another onto a
    page already read. One in five hundred, and always less than a page.
    """
    return min(PAGE_SIZE - 1, announced // 500)


def _printed_text(page_html: str, job_id: str) -> str | None:
    """The text of a vacancy page that carries no schema.org data.

    Its description tag holds what the data holds on the pages that have it.
    Only the page of this vacancy counts, which links to its application: a
    company's own page has a description tag too.
    """
    apply_path = f"/Apply/{job_id}"
    text: str | None = None
    is_vacancy = False
    for node in parse_html(page_html).walk():
        if node.tag == "meta" and node.attrs.get("name") == "description":
            # Plain text, already decoded: nothing in it is markup.
            text = node.attrs.get("content", "").strip()[:25_000] or None
        elif node.tag == "a" and node.attrs.get("href", "").split("?")[0] == apply_path:
            is_vacancy = True
    return text if is_vacancy else None


def _site_name(tree: Node) -> str | None:
    heading = tree.find("h1")
    name = heading.text() if heading is not None else ""
    if not name:
        title = tree.find("title")
        name = _SITE_SUFFIX.sub("", title.text()) if title is not None else ""
    return " ".join(name.split()) or None
