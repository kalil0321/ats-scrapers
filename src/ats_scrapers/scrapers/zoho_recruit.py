"""Zoho Recruit career sites.

An employer on Zoho Recruit publishes a career site at
``https://{tenant}.zohorecruit.{tld}/jobs/{portal}`` (the portal is usually
``Careers``). The page carries every published job, with its text, in a
hidden ``<input id="jobs">`` and the employer in ``<input id="meta">``::

    GET /jobs/{portal}          -> all jobs of the portal
    GET /jobs/{portal}/{id}     -> the page of one job
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

DEFAULT_PORTAL = "Careers"

_HOST_RE = re.compile(
    r"[a-z0-9][a-z0-9-]*\.zohorecruit\.(?:com|eu|in|jp|sa|ca|com\.au|com\.cn)"
)
_PORTAL_RE = re.compile(r"[A-Za-z0-9._~%-]+")
# Checked in order: "Temps plein" (full time, in French) must not read as
# temporary.
_EMPLOYMENT_TYPES: tuple[tuple[str, EmploymentType], ...] = (
    ("intern", "INTERN"),
    ("stage", "INTERN"),
    ("part", "PART_TIME"),
    ("teilzeit", "PART_TIME"),
    ("full", "FULL_TIME"),
    ("plein", "FULL_TIME"),
    ("completo", "FULL_TIME"),
    ("vollzeit", "FULL_TIME"),
    ("permanent", "FULL_TIME"),
    ("contract", "CONTRACT"),
    ("freelance", "CONTRACT"),
    ("temporar", "TEMPORARY"),
    ("temporaire", "TEMPORARY"),
    ("seasonal", "TEMPORARY"),
)
_JS_PARSE_RE = re.compile(r"JSON\.parse\('((?:[^'\\]|\\.)*)'\)")
_JS_ESCAPE_RE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.DOTALL)
_JS_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
DETAIL_CONCURRENCY = 4
# A career site page carries at most this many jobs, and no parameter reaches
# the others.
LIST_CAP = 750
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
_MISSING_PAGE_RE = re.compile(r"<title>\s*Page does not exist\s*</title>", re.IGNORECASE)


@ScraperRegistry.register(ATSType.ZOHO_RECRUIT)
class ZohoRecruitScraper(BaseScraper):
    """One Zoho Recruit career site.

    ``company_slug`` is the career site URL
    (``https://acme.zohorecruit.com/jobs/Careers``) or its host, in which
    case the default portal ``Careers`` is read.
    """

    ats = ATSType.ZOHO_RECRUIT

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        host, portal = _split_slug(company_slug)
        self.portal_url = f"https://{host}/jobs/{portal}"
        super().__init__(
            self.portal_url,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_name = (company_name or "").strip() or None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            page = await fetch.get_text(self.portal_url)
            postings = _hidden_json(page, "jobs")
            if not isinstance(postings, list):
                if _MISSING_PAGE_RE.search(page):
                    raise CompanyNotFoundError(
                        f"Zoho Recruit page {self.portal_url} does not exist"
                    )
                raise ScraperError(
                    f"Zoho Recruit page {self.portal_url} carries no list of jobs"
                )
            if len(postings) >= LIST_CAP:
                logger.warning(
                    "Zoho Recruit page %s lists %d jobs, the most a page carries; "
                    "the site may have more",
                    self.portal_url,
                    len(postings),
                )
            company = (
                self.company_name or _company_name(page) or _tenant(self.portal_url)
            )
            jobs = [
                job
                for item in postings
                if isinstance(item, dict)
                and (job := self._parse_job(item, company)) is not None
            ]
            if self.include_descriptions:
                jobs = await self._add_missing_descriptions(fetch, jobs)
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        return self._run_sync(self._description(str(job.url)))

    async def _description(self, url: str) -> str | None:
        async with self.make_fetcher() as fetch:
            record = await _read_job_page(fetch, url)
        return _description_text(record.get("Job_Description")) if record else None

    async def _add_missing_descriptions(self, fetch: Fetcher, jobs: list[Job]) -> list[Job]:
        """Some portals list jobs without their text; it is on the job page.

        Returns the jobs whose page still exists.
        """
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
        closed: set[str | None] = set()

        async def fill(job: Job) -> None:
            async with semaphore:
                try:
                    record = await _read_job_page(fetch, str(job.url))
                except CompanyNotFoundError:
                    # Closed since the list was read.
                    closed.add(job.ats_id)
                    return
                except ScraperError as exc:
                    # The job stays, without its text.
                    logger.warning("Zoho Recruit job %s could not be read: %s", job.url, exc)
                    return
            if not record:
                return
            if description := _description_text(record.get("Job_Description")):
                job.description = description
            if job.posted_at is None:
                job.posted_at = _parse_date(record.get("Date_Opened"))

        await asyncio.gather(*(fill(job) for job in jobs if not job.description))
        return [job for job in jobs if job.ats_id not in closed]

    def _parse_job(self, item: dict[str, Any], company: str) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = _text(item.get("Posting_Title")) or _text(item.get("Job_Opening_Name"))
        if not ats_id or not title or item.get("Publish") is False:
            return None
        place = [
            part
            for key in ("City", "State", "Country")
            if (part := _text(item.get(key)))
        ]
        job_type = _text(item.get("Job_Type"))
        raw: dict[str, Any] = {}
        if industry := _text(item.get("Industry")):
            raw["industry"] = industry
        return Job(
            url=HttpUrl(f"{self.portal_url}/{ats_id}"),
            title=title,
            company=company,
            ats_type=ATSType.ZOHO_RECRUIT,
            ats_id=ats_id,
            location=", ".join(dict.fromkeys(place)) or None,
            is_remote=True if item.get("Remote_Job") is True else None,
            employment_type=_employment_type(job_type),
            commitment=job_type,
            description=(
                _description_text(item.get("Job_Description"))
                if self.include_descriptions
                else None
            ),
            posted_at=_parse_date(item.get("Date_Opened")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


def _split_slug(company_slug: str) -> tuple[str, str]:
    """``(host, portal)`` of a career site URL or host."""
    slug = company_slug.strip()
    parsed = urlparse(slug if "://" in slug else f"https://{slug}")
    host = (parsed.hostname or "").lower()
    if not _HOST_RE.fullmatch(host):
        raise ScraperError(
            f"ZohoRecruitScraper needs a *.zohorecruit.* career site, got {company_slug!r}"
        )
    segments = [part for part in parsed.path.split("/") if part]
    portal = DEFAULT_PORTAL
    if len(segments) >= 2 and segments[0].lower() == "jobs":
        portal = segments[1]
    if not _PORTAL_RE.fullmatch(portal):
        raise ScraperError(f"ZohoRecruitScraper got an unsafe portal name {portal!r}")
    return host, portal


def _hidden_json(page: str, element_id: str) -> Any:
    """The JSON held by ``<input type="hidden" id="{element_id}">``, or ``None``."""
    for tag in re.findall(r"<input\b[^>]*>", page):
        if re.search(rf'\bid="{re.escape(element_id)}"', tag):
            value = re.search(r'\bvalue="([^"]*)"', tag)
            if not value:
                return None
            try:
                return json.loads(html.unescape(value.group(1)))
            except ValueError as exc:
                raise ScraperError(
                    f"Zoho Recruit page holds unreadable {element_id!r} data"
                ) from exc
    return None


async def _read_job_page(fetch: Fetcher, url: str) -> dict[str, Any] | None:
    """The job behind a job page; a page that is gone is not found."""
    page = await fetch.get_text(url)
    record = _job_page_record(page)
    if record is None and _MISSING_PAGE_RE.search(page):
        raise CompanyNotFoundError(f"Zoho Recruit job page {url} does not exist")
    return record


def _job_page_record(page: str) -> dict[str, Any] | None:
    """The job a job page was rendered from, or ``None``.

    The page holds it as ``JSON.parse('[{...}]')``, a JavaScript string.
    """
    for literal in _JS_PARSE_RE.findall(page):
        if "Posting_Title" not in literal:
            continue
        try:
            records = json.loads(_JS_ESCAPE_RE.sub(_js_unescape, literal))
        except ValueError:
            continue
        if isinstance(records, list) and records and isinstance(records[0], dict):
            return records[0]
    return None


def _js_unescape(match: re.Match[str]) -> str:
    escape = match.group(1)
    if len(escape) > 1:
        return chr(int(escape[1:], 16))
    return _JS_SIMPLE_ESCAPES.get(escape, escape)


def _company_name(page: str) -> str | None:
    try:
        meta = _hidden_json(page, "meta")
    except ScraperError:
        return None
    org = meta.get("org_info") if isinstance(meta, dict) else None
    return _text(org.get("company_name")) if isinstance(org, dict) else None


def _tenant(portal_url: str) -> str:
    return (urlparse(portal_url).hostname or "").split(".", 1)[0]


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _description_text(value: object) -> str | None:
    """A job text as plain text; job pages hold it as HTML."""
    text = _text(value)
    if text and _HTML_TAG_RE.search(text):
        text = parse_html(text).lines().strip() or None
    return text[:25_000] if text else None


def _employment_type(job_type: str | None) -> EmploymentType | None:
    if not job_type:
        return None
    normalized = job_type.lower()
    return next((mapped for needle, mapped in _EMPLOYMENT_TYPES if needle in normalized), None)


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
