"""HiringThing job boards, under its own name or a partner's.

The platform also runs the boards that payroll and HR providers offer under
their own domains, so a board is named by its address:
``https://acme.hiringthing.com`` or ``https://acme.partner-domain.com``. A
bare name is taken as a ``hiringthing.com`` board.

The front page of a board lists every open job. The description, the date
and the postal address come from each job's page.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job, SalaryPeriod
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._posting_detail import body_text, fill_missing, job_posting
from ats_scrapers.scrapers._posting_fields import finite_number
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher
    from ats_scrapers.scrapers._htmltree import Node

DETAIL_CONCURRENCY = 4

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_JOB_PATH = re.compile(r"^/job/(\d+)(?:/[^?#]*)?$")
_FILTERS = "HiringThing.Components.JobFiltersContainer"
_SALARY = "HiringThing.Components.JobSalary"
_SITE_SUFFIX = re.compile(r"\s+Career Opportunities$", re.IGNORECASE)
_PERIODS: dict[str, SalaryPeriod] = {
    "hourly": "HOUR", "daily": "DAY", "weekly": "WEEK", "monthly": "MONTH",
    "annually": "YEAR", "yearly": "YEAR",
}


@ScraperRegistry.register(ATSType.HIRINGTHING)
class HiringThingScraper(BaseScraper):
    """Scrape one public HiringThing job board."""

    ats = ATSType.HIRINGTHING

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
        slug = company_slug.strip()
        if slug.startswith(("http://", "https://")):
            host = urlparse(require_http_url(slug, provider="HiringThingScraper")).hostname
            self.host = (host or "").lower()
        else:
            label = require_host_label(slug, provider="HiringThingScraper").lower()
            self.host = f"{label}.hiringthing.com"
        if self.host.startswith("www."):
            raise ScraperError("HiringThing requires a board's own address")
        self.company_slug = f"https://{self.host}"

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher(follow_redirects=False) as fetch:
            response = await fetch.request("GET", f"{self.company_slug}/", handled=_REDIRECTS)
            if response.status_code in _REDIRECTS:
                # An unknown board is sent to the provider's own site.
                raise CompanyNotFoundError(f"HiringThing ({self.host}) has no board")
            jobs = self._parse_board(response.text)
            if self.include_descriptions:
                await fill_missing(jobs, lambda job: self._detail(fetch, job), DETAIL_CONCURRENCY)
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher(follow_redirects=False) as fetch:
                await self._detail(fetch, job)
            return job.description

        return self._run_sync(run())

    def _parse_board(self, page_html: str) -> list[Job]:
        tree = parse_html(page_html)
        filters: dict[str, Any] | None = None
        cards: list[Node] = []
        for node in tree.walk():
            if node.attrs.get("data-react-class") == _FILTERS:
                filters = _props(node) or {}
            elif "job-container" in node.classes and node.attrs.get("data-job-id"):
                cards.append(node)
        if filters is None:
            raise ScraperError(f"HiringThing ({self.host}) returned an unrecognized page")
        remote = {str(job_id) for job_id in filters.get("remotePositions") or []}
        company = _company(tree) or self.host
        jobs: dict[str, Job] = {}
        for card in cards:
            path = next(
                (
                    link.attrs.get("href", "") for link in card.walk()
                    if link.tag == "a" and _JOB_PATH.match(link.attrs.get("href", ""))
                ),
                "",
            )
            match = _JOB_PATH.match(path)
            heading = card.find("h2")
            title = heading.text() if heading is not None else ""
            if not match or match.group(1) != card.attrs["data-job-id"] or not title:
                continue
            job_id = match.group(1)
            url = f"{self.company_slug}{path}"
            place = _by_class(card, "job-location")
            jobs.setdefault(job_id, Job(
                url=url,
                title=title,
                company=company,
                ats_type=self.ats,
                ats_id=job_id,
                location=(place.text() if place is not None else "") or None,
                is_remote=True if job_id in remote else None,
                apply_url=url,
                fetched_at=datetime.now(UTC),
                **_salary(card),
            ))
        return list(jobs.values())

    async def _detail(self, fetch: Fetcher, job: Job) -> None:
        response = await fetch.request(
            "GET", str(job.url), handled=_REDIRECTS | frozenset({404, 410}),
        )
        if response.status_code != 200:
            return
        posting = job_posting(response.text)
        if not posting:
            return
        job.description = body_text(posting.get("description"))[:25_000] or None
        job.posted_at = _iso_datetime(posting.get("datePosted")) or job.posted_at
        address = _address(posting.get("jobLocation"))
        country = address.get("addressCountry")
        if isinstance(country, str) and len(country.strip()) == 2 and country.strip().isalpha():
            job.country_iso = country.strip().upper()
        postal = address.get("postalCode")
        if isinstance(postal, str) and postal.strip():
            job.raw = {**(job.raw or {}), "postal_code": postal.strip()}


def _by_class(node: Node, name: str) -> Node | None:
    return next((child for child in node.walk() if name in child.classes), None)


def _props(node: Node) -> dict[str, Any] | None:
    try:
        props = json.loads(node.attrs.get("data-react-props", ""))
    except ValueError:
        return None
    return props if isinstance(props, dict) else None


def _company(tree: Node) -> str | None:
    for node in tree.walk():
        if node.tag == "meta" and node.attrs.get("property") == "og:title":
            name = " ".join(node.attrs.get("content", "").split())
            if name:
                return name
    title = tree.find("title")
    return _SITE_SUFFIX.sub("", title.text()) or None if title is not None else None


def _salary(card: Node) -> dict[str, Any]:
    props = next(
        (
            _props(node) for node in card.walk()
            if node.attrs.get("data-react-class") == _SALARY
        ),
        None,
    )
    if not props:
        return {}
    low, high = props.get("minSalary"), props.get("maxSalary")
    minimum = _amount(low)
    maximum = _amount(high)
    if minimum is None and maximum is None:
        return {}
    currency = next(
        (
            side["currency"].upper() for side in (low, high)
            if isinstance(side, dict) and isinstance(side.get("currency"), str)
            and len(side["currency"]) == 3 and side["currency"].isalpha()
        ),
        None,
    )
    period = _PERIODS.get(str(props.get("payFrequency") or "").lower())
    if minimum is not None and maximum is not None and minimum != maximum:
        summary = f"{minimum:g} - {maximum:g}"
    else:
        summary = f"{minimum if minimum is not None else maximum:g}"
    if period:
        summary = f"{summary} per {period.casefold()}"
    return {
        "salary_currency": currency,
        "salary_period": period,
        "salary_summary": summary,
        "salary_min": minimum,
        "salary_max": maximum,
    }


def _amount(side: object) -> float | None:
    amount = finite_number(side.get("amount")) if isinstance(side, dict) else None
    return amount if amount is not None and amount > 0 else None


def _address(place: object) -> dict[str, Any]:
    """The postal address of a job page, which prints it as a quoted record."""
    if isinstance(place, str):
        if len(place) > 4_000:
            return {}
        try:
            place = ast.literal_eval(place)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return {}
    address = place.get("address") if isinstance(place, dict) else None
    return address if isinstance(address, dict) else {}


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
