"""MyCareersFuture: Singapore's public job portal.

The portal publishes every open posting through one public JSON feed::

    GET https://api.mycareersfuture.gov.sg/v2/jobs?limit=100&page=N

Each page carries the full posting, text included, so no detail request is
needed. ``company_slug`` is ignored: the feed is one source for the country.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API = "https://api.mycareersfuture.gov.sg/v2/jobs"
SITE = "https://www.mycareersfuture.gov.sg"
PAGE_SIZE = 100
MAX_CONCURRENCY = 4
MAX_PAGES = 5_000
# Postings open and close while the feed is read, so a few are missed or
# seen twice. Far fewer than announced means the read went wrong.
MIN_COMPLETE = 0.9

# Most specific first: a posting often carries several labels.
_EMPLOYMENT: tuple[tuple[str, EmploymentType], ...] = (
    ("internship", "INTERN"),
    ("part time", "PART_TIME"),
    ("temporary", "TEMPORARY"),
    ("contract", "CONTRACT"),
    ("freelance", "CONTRACT"),
    ("full time", "FULL_TIME"),
    ("permanent", "FULL_TIME"),
)
_SALARY_PERIOD = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "annual": "YEAR",
    "annually": "YEAR",
    "yearly": "YEAR",
}
_DISTRICT_CODE_RE = re.compile(r"^D\d{1,2}\s+")


@ScraperRegistry.register(ATSType.MYCAREERSFUTURE)
class MyCareersFutureScraper(BaseScraper):
    """Singapore public job portal, one feed. ``company_slug`` is ignored."""

    ats = ATSType.MYCAREERSFUTURE

    default_headers: ClassVar[dict[str, str] | None] = {
        "Accept": "application/json",
    }

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        # Every posting read, kept or not: what the feed's total is checked against.
        fetched: set[str] = set()

        def absorb(items: list[dict[str, Any]]) -> None:
            for item in items:
                fetched.add(str(item.get("uuid") or id(item)))
                job = self._parse(item)
                if job is not None and job.ats_id:
                    jobs.setdefault(job.ats_id, job)

        async with self.make_fetcher() as fetch:
            first, total = await _page(fetch, 0)
            absorb(first)
            if not first:
                if total:
                    raise ScraperError(f"MyCareersFuture returned 0 of {total} postings")
                return []
            last = min(-(-total // PAGE_SIZE), MAX_PAGES)
            semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

            async def read(number: int) -> None:
                async with semaphore:
                    items, _ = await _page(fetch, number)
                absorb(items)

            await asyncio.gather(*(read(number) for number in range(1, last)))
            # The feed may have grown while it was read.
            number = last
            while number < MAX_PAGES:
                items, _ = await _page(fetch, number)
                if not items:
                    break
                absorb(items)
                number += 1

        if len(fetched) < total * MIN_COMPLETE:
            raise ScraperError(
                f"MyCareersFuture returned {len(fetched)} of {total} postings"
            )
        return list(jobs.values())

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("uuid") or "").strip()
        title = str(item.get("title") or "").strip()
        metadata = _dict(item.get("metadata"))
        company = _employer(item, metadata)
        if not ats_id or not title or not company:
            return None
        url = str(metadata.get("jobDetailsUrl") or "").strip()
        if not url.startswith(f"{SITE}/"):
            url = f"{SITE}/job/{ats_id}"
        location, country_iso, lat, lon = _place(_dict(item.get("address")))
        labels = _labels(item.get("employmentTypes"), "employmentType")
        salary = {} if metadata.get("isHideSalary") else _dict(item.get("salary"))
        salary_min, salary_max = _number(salary.get("minimum")), _number(salary.get("maximum"))
        period_label = str(_dict(salary.get("type")).get("salaryType") or "")
        has_salary = salary_min is not None or salary_max is not None
        experience = item.get("minimumYearsExperience")
        categories = _labels(item.get("categories"), "category")
        html = item.get("description")
        description = parse_html(html).lines().strip() if isinstance(html, str) else ""
        raw: dict[str, object] = {
            key: value
            for key, value in (
                ("position_levels", _labels(item.get("positionLevels"), "position")),
                ("categories", categories),
                ("vacancies", item.get("numberOfVacancies")),
                ("posted_by", _posted_by(item, metadata, company)),
            )
            if value
        }
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=self.ats,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Asia" if country_iso == "SG" else None,
            lat=lat,
            lon=lon,
            salary_currency="SGD" if has_salary else None,
            salary_period=_SALARY_PERIOD.get(period_label.lower()) if has_salary else None,
            salary_summary=_salary_summary(salary_min, salary_max, period_label),
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience if isinstance(experience, int) and experience >= 0 else None,
            employment_type=_employment_type(labels),
            department=categories[0] if categories else None,
            requisition_id=str(metadata.get("jobPostId") or "").strip() or None,
            commitment=", ".join(labels) or None,
            description=description[:25_000] or None,
            posted_at=_date(metadata.get("newPostingDate")),
            application_deadline=_deadline(metadata.get("expiryDate")),
            language="en",
            raw=raw or None,
        )


async def _page(fetch: Fetcher, number: int) -> tuple[list[dict[str, Any]], int]:
    """One page of postings and the total the feed announces."""
    data = await fetch.get_json(API, params={"limit": PAGE_SIZE, "page": number})
    results = data.get("results") if isinstance(data, dict) else None
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(results, list) or isinstance(total, bool) or not isinstance(total, int):
        raise ScraperError(f"MyCareersFuture page {number} is not a list of postings")
    return [item for item in results if isinstance(item, dict)], max(total, 0)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _labels(items: object, key: str) -> list[str]:
    if not isinstance(items, list):
        return []
    labels = (str(_dict(item).get(key) or "").strip() for item in items)
    return list(dict.fromkeys(label for label in labels if label))


def _name(company: object) -> str:
    return str(_dict(company).get("name") or "").strip()


def _employer(item: dict[str, Any], metadata: dict[str, Any]) -> str:
    """The hiring employer, or the company that posted when it is not named."""
    if not metadata.get("isHideHiringEmployerName"):
        hiring = _name(item.get("hiringCompany"))
        if hiring:
            return hiring
    if metadata.get("isHideEmployerName"):
        return ""
    return _name(item.get("postedCompany"))


def _posted_by(
    item: dict[str, Any], metadata: dict[str, Any], company: str
) -> str | None:
    """The company that posted for the employer, unless it asked not to be named."""
    if metadata.get("isHideEmployerName"):
        return None
    posted = _name(item.get("postedCompany"))
    return posted if posted and posted != company else None


def _place(
    address: dict[str, Any],
) -> tuple[str | None, str | None, float | None, float | None]:
    country = str(address.get("overseasCountry") or "").strip()
    if address.get("isOverseas") and country and country.lower() != "singapore":
        return country, None, None, None
    areas = list(dict.fromkeys(
        area
        for district in _labels(address.get("districts"), "location")
        if (area := _DISTRICT_CODE_RE.sub("", district)) and area.lower() != "islandwide"
    ))
    location = "; ".join(f"{area}, Singapore" for area in areas) or "Singapore"
    lat, lon = _number(address.get("lat")), _number(address.get("lng"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    return location, "SG", lat, lon


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _salary_summary(low: float | None, high: float | None, period: str) -> str | None:
    amounts = [f"{amount:,.0f}" for amount in (low, high) if amount is not None]
    if not amounts:
        return None
    span = " - ".join(dict.fromkeys(amounts))
    return f"SGD {span} {period}".strip()


def _employment_type(labels: list[str]) -> EmploymentType | None:
    lowered = [label.lower() for label in labels]
    for needle, value in _EMPLOYMENT:
        if any(needle in label for label in lowered):
            return value
    return None


def _date(value: object) -> datetime | None:
    """A posting day, as the start of that day in UTC."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _deadline(value: object) -> datetime | None:
    """A closing day stays a day: no time zone is added to a bare date."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None
