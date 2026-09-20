"""Varbi employer RSS feeds with public job-detail metadata.

Only employer-specific ``{tenant}.varbi.com`` boards are supported. Feeds
provide complete descriptions; detail pages add location, employment terms,
requisitions and application dates without credentials. Date-only deadlines
are retained as dates in ``raw``, rather than inventing a closing time.
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

_JOB_PATH = re.compile(r"^/(?:[a-z]{2}/)?what:job/jobID:(\d+)/?$")
_NON_VACANCY_TITLE = re.compile(
    r"\((?:scholarship|stipendium)\)|^(?:intresseanmälan|spontanansökan|"
    r"general application|open application|expression of interest|"
    r"open sollicitatie|spontane sollicitatie|uopfordret ansøgning|"
    r"spontanansøgning)\b", re.I,
)
_COMPANY_PREFIX = re.compile(
    r"^(?:New jobs at|Nya lediga jobb hos|Lediga jobb hos|Ledige stillinger hos|"
    r"Ledige job hos|Nye ledige stillinger hos|Nieuwe vacatures bij)\s+", re.I,
)
_COUNTRIES = {
    "sweden": "SE", "sverige": "SE", "denmark": "DK", "danmark": "DK",
    "norway": "NO", "norge": "NO", "finland": "FI", "suomi": "FI",
    "iceland": "IS", "island": "IS", "germany": "DE", "tyskland": "DE",
    "netherlands": "NL", "nederländerna": "NL", "nederland": "NL",
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "maj": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10,
    "nov": 11, "dec": 12, "des": 12,
    "january": 1, "januari": 1, "february": 2, "februari": 2,
    "march": 3, "mars": 3, "april": 4, "june": 6, "juni": 6,
    "july": 7, "juli": 7, "august": 8, "augusti": 8, "september": 9,
    "october": 10, "oktober": 10, "november": 11, "december": 12,
}
_METADATA_FIELDS = (
    "type-of-employment", "hours", "town", "county", "country",
    "reference-number", "published", "ends",
)


@ScraperRegistry.register(ATSType.VARBI)
class VarbiScraper(BaseScraper):
    """Fetch one public Varbi employer, not Varbi's aggregate job board."""

    ats = ATSType.VARBI
    default_headers: ClassVar[dict[str, str]] = {"User-Agent": "Mozilla/5.0"}

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        company_name: str | None = None,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug, timeout=timeout, include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = require_host_label(company_slug, provider="VarbiScraper").lower()
        if self.company_slug in {"www", "api", "support", "login"}:
            raise ScraperError("Varbi requires an employer-specific tenant")
        self.host = f"{self.company_slug}.varbi.com"
        self.feed_url = f"https://{self.host}/what:rssfeed/"
        self.company_name = (company_name or "").strip() or None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher(follow_redirects=False) as fetch:
            response = await fetch.request("GET", self.feed_url)
            if response.status_code != 200:
                raise ScraperError(f"Varbi feed returned {response.status_code}")
            jobs = self._parse_feed(response.text)
            if not self.include_descriptions:
                for job in jobs:
                    job.description = None
                return jobs
            semaphore = asyncio.Semaphore(4)
            results = await asyncio.gather(*(
                self._enrich_detail(fetch, semaphore, job) for job in jobs
            ), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        return [job for job in results if isinstance(job, Job)]

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher(follow_redirects=False) as fetch:
                response = await fetch.request("GET", self.feed_url)
            if response.status_code != 200:
                raise ScraperError(f"Varbi feed returned {response.status_code}")
            return next(
                (item.description for item in self._parse_feed(response.text)
                 if item.ats_id == job.ats_id), None,
            )

        return self._run_sync(run())

    def _parse_feed(self, xml_text: str) -> list[Job]:
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.I):
            raise ScraperError("Varbi feed contains an unsupported XML declaration")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as error:
            raise ScraperError("Varbi returned malformed RSS") from error
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is None:
            raise ScraperError("Varbi returned an unrecognized RSS feed")
        heading = _text(channel.findtext("title", ""))
        company = self.company_name
        if company is None and _COMPANY_PREFIX.match(heading):
            company = _COMPANY_PREFIX.sub("", heading).strip()
        if not company:
            raise ScraperError("Varbi feed omitted a readable employer name")
        jobs: dict[str, Job] = {}
        for item in channel.findall("item"):
            title = _text(item.findtext("title", ""))
            url = (item.findtext("link") or "").strip()
            parsed = urlsplit(url)
            match = _JOB_PATH.fullmatch(parsed.path)
            if (
                parsed.scheme != "https" or parsed.netloc.lower() != self.host
                or parsed.query or parsed.fragment or not match
            ):
                raise ScraperError(f"Varbi feed contains an untrusted job URL: {url}")
            description = _description_text(item.findtext("description", ""))
            if not title or not description:
                raise ScraperError("Varbi feed omitted a job title or description")
            if _NON_VACANCY_TITLE.search(title):
                continue
            job_id = match[1]
            if job_id in jobs:
                continue
            jobs[job_id] = Job(
                ats_type=self.ats, ats_id=f"{self.company_slug}:{job_id}",
                title=title, company=company, url=HttpUrl(url),
                description=description[:25_000],
                posted_at=_posted_at(item.findtext("pubDate", "")),
                raw={"source_feed": self.feed_url},
            )
        return list(jobs.values())

    async def _enrich_detail(
        self, fetch: Fetcher, semaphore: asyncio.Semaphore, job: Job,
    ) -> Job | None:
        async with semaphore:
            response = await fetch.request(
                "GET", str(job.url), handled=frozenset({404, 410}),
            )
        if response.status_code in {404, 410}:
            return None
        if response.status_code != 200:
            raise ScraperError(f"Varbi detail returned {response.status_code}")
        return self._apply_detail(job, response.text)

    def _apply_detail(self, job: Job, html_text: str) -> Job | None:
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise ScraperError("Varbi detail parsing requires ats-scrapers[scrapers]") from error
        soup = BeautifulSoup(html_text, "html.parser")
        heading = soup.select_one("h1")
        identity = soup.select_one('meta[property="og:url"]')
        if (
            heading is None or soup.select_one("table.quick-info") is None
            or not heading.get_text(strip=True)
            or identity is None or identity.get("content") != str(job.url)
        ):
            raise ScraperError(f"Varbi detail did not match the listed job: {job.url}")
        metadata = {}
        for field in _METADATA_FIELDS:
            cell = soup.select_one(f".quick-info-{field} td")
            if cell is not None:
                metadata[field] = cell.get_text(" ", strip=True)
        deadline_text = metadata.get("ends", "")
        deadline = _date(deadline_text)
        if deadline_text and deadline is None:
            raise ScraperError(f"Varbi returned an unrecognized deadline: {deadline_text}")
        if deadline and deadline < datetime.now(ZoneInfo("Europe/Stockholm")).date():
            return None
        job.location = ", ".join(
            dict.fromkeys(metadata[field] for field in ("town", "county", "country")
                          if metadata.get(field))
        ) or None
        job.country_iso = _COUNTRIES.get(metadata.get("country", "").casefold())
        if job.country_iso:
            job.region = "Europe"
        job.requisition_id = metadata.get("reference-number") or None
        job.commitment = metadata.get("type-of-employment") or None
        job.employment_type = _employment_type(metadata)
        raw = dict(job.raw or {})
        raw["metadata"] = metadata
        if deadline:
            raw["application_deadline_date"] = deadline.isoformat()
        job.raw = raw
        job_id = str(job.ats_id).rsplit(":", 1)[-1]
        for anchor in soup.select("a[href]"):
            url = str(anchor.get("href") or "")
            parsed = urlsplit(url)
            if (
                parsed.scheme == "https" and parsed.netloc.lower() == self.host
                and not parsed.query and not parsed.fragment
                and re.fullmatch(
                    rf"/(?:[a-z]{{2}}/)?(?:what:login/jobID:{re.escape(job_id)}/type:job/apply:1"
                    rf"|apply/positionquick/{re.escape(job_id)})/",
                    parsed.path,
                )
            ):
                job.apply_url = HttpUrl(url)
                break
        return job


def _text(value: str) -> str:
    return html.unescape(html.unescape(value)).strip()


def _description_text(value: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError as error:
        raise ScraperError("Varbi description parsing requires ats-scrapers[scrapers]") from error
    soup = BeautifulSoup(_text(value), "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return soup.get_text(" ", strip=True)


def _posted_at(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _date(value: str) -> date | None:
    value = value.strip()
    numeric = re.fullmatch(r"(\d{2})[-.](\d{2})[-.](\d{4})", value)
    if numeric:
        try:
            return date(int(numeric[3]), int(numeric[2]), int(numeric[1]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        match = re.fullmatch(
            r"(\d{1,2})[.\s]+([a-z]+)[.\s]+(\d{4})", value.casefold(),
        )
        if match and match[2] in _MONTHS:
            try:
                return date(int(match[3]), _MONTHS[match[2]], int(match[1]))
            except ValueError:
                pass
        return None


def _employment_type(metadata: dict[str, str]) -> EmploymentType | None:
    contract = metadata.get("type-of-employment", "").casefold()
    hours = metadata.get("hours", "").casefold()
    if contract.strip() in {"contract", "contractor"}:
        return "CONTRACT"
    if any(word in contract for word in ("internship", "praktik")):
        return "INTERN"
    if any(word in contract for word in ("temporary", "vikariat", "visstid", "tidsbegräns", "tidsbegrænset", "midlertidig")) or contract.startswith("bepaalde tijd"):
        return "TEMPORARY"
    part_time = any(word in hours for word in ("part time", "part-time", "parttime", "deltid"))
    full_time = any(word in hours for word in ("full time", "full-time", "fulltime", "heltid", "fuldtid"))
    if part_time and full_time:
        return None
    if part_time:
        return "PART_TIME"
    if full_time:
        return "FULL_TIME"
    return None
