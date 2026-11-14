"""Emploitic: the job board of Algeria.

The site lists its open jobs in ``/sitemap-jobs.xml`` and each job page
carries a schema.org ``JobPosting`` record. ``company_slug`` is ignored.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers._listings_board import (
    _EMPLOYMENT,
    _SALARY_PERIODS,
    _dict,
    _nodes,
    _number,
    _salary_summary,
    _text,
    _time,
)
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

HOST = "emploitic.com"
SITEMAP = f"https://{HOST}/sitemap-jobs.xml"
DETAIL_CONCURRENCY = 4
# Jobs close while the site is read; more unreadable pages than this share
# means the site changed or refused the read.
MAX_UNREADABLE_SHARE = 0.1
COUNTRY_NAMES = {"DZ": "Algeria"}

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


@ScraperRegistry.register(ATSType.EMPLOITIC)
class EmploiticScraper(BaseScraper):
    """Emploitic, one source. ``company_slug`` is ignored."""

    ats = ATSType.EMPLOITIC

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            urls = list(dict.fromkeys(
                url
                for url in _LOC_RE.findall(await fetch.get_text(SITEMAP))
                if _is_job_page(url)
            ))
            if not urls:
                raise ScraperError("Emploitic lists no job page")
            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            jobs: dict[str, Job] = {}
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
                job = self._parse(page, url)
                if job is None:
                    unreadable.append(url)
                elif job.ats_id:
                    jobs.setdefault(job.ats_id, job)

            await asyncio.gather(*(read(url) for url in urls))
        if len(unreadable) > len(urls) * MAX_UNREADABLE_SHARE:
            raise ScraperError(
                f"Emploitic: {len(unreadable)} of {len(urls)} job pages "
                "could not be read"
            )
        return list(jobs.values())

    def _parse(self, page: str, url: str) -> Job | None:
        posting = next(
            (node for node in _nodes(page).values() if node.get("@type") == "JobPosting"),
            None,
        )
        if posting is None:
            return None
        title = _text(posting.get("title"))
        company = _text(_dict(posting.get("hiringOrganization")).get("name"))
        if not title or not company:
            return None
        key = _text(_dict(posting.get("identifier")).get("value"))
        places, countries = _places(posting.get("jobLocation"))
        salary = _dict(posting.get("baseSalary"))
        amounts = _dict(salary.get("value"))
        low = _number(amounts.get("minValue")) or _number(amounts.get("value"))
        high = _number(amounts.get("maxValue")) or low
        currency = _text(salary.get("currency")) if low is not None else None
        period = str(amounts.get("unitText") or "").upper()
        html = posting.get("description")
        description = None
        if self.include_descriptions and isinstance(html, str):
            description = parse_html(html).lines().strip()[:25_000] or None
        education = _dict(posting.get("educationRequirements"))
        raw = {
            name: value
            for name, value in (
                ("experience", _text(posting.get("experienceRequirements"))),
                ("education", _text(education.get("credentialCategory"))),
            )
            if value
        }
        country_iso = countries[0] if len(countries) == 1 else None
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=self.ats,
            ats_id=key or urlparse(url).path.rsplit("/", 1)[-1],
            location="; ".join(places) or None,
            country_iso=country_iso,
            region="Africa" if country_iso == "DZ" else None,
            is_remote=True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
            salary_currency=currency,
            salary_period=period if low is not None and period in _SALARY_PERIODS else None,
            salary_summary=_salary_summary(currency, low, high, period),
            salary_min=low,
            salary_max=high,
            employment_type=_employment_type(posting.get("employmentType")),
            description=description,
            posted_at=_time(posting.get("datePosted")),
            application_deadline=_time(posting.get("validThrough")),
            raw=raw or None,
        )


def _is_job_page(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == HOST
        and "/offres-d-emploi/" in parsed.path
    )


def _places(value: object) -> tuple[list[str], list[str]]:
    """The places of a posting as text, and the countries they are in."""
    items = value if isinstance(value, list) else [value]
    places: dict[str, None] = {}
    countries: dict[str, None] = {}
    for item in items:
        address = _dict(_dict(item).get("address"))
        code = (_text(address.get("addressCountry")) or "").upper()
        if re.fullmatch(r"[A-Z]{2}", code):
            countries.setdefault(code)
        parts = dict.fromkeys(
            part
            for part in (
                _text(address.get("addressLocality")),
                _text(address.get("addressRegion")),
                COUNTRY_NAMES.get(code),
            )
            if part
        )
        if parts:
            places.setdefault(", ".join(parts))
    return list(places), list(countries)


def _employment_type(value: object) -> Any:
    labels = value if isinstance(value, list) else [value]
    for label in labels:
        if isinstance(label, str) and label.upper() in _EMPLOYMENT:
            return _EMPLOYMENT[label.upper()]
    return None
