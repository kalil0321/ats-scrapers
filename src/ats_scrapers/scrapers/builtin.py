"""Built In public listings, with explicit pagination and no paid services."""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

API_ROOT = "https://builtin.com"
DEFAULT_MAX_PAGES = 2000
_JOB_URL_ID_RE = re.compile(r"^https://builtin\.com/job/[^/]+/(?P<id>\d+)/?$")


@ScraperRegistry.register(ATSType.BUILTIN)
class BuiltInScraper(BaseScraper):
    """Reject failed, repeated or capped runs instead of returning partial jobs.

    Listing descriptions are labelled summaries. When descriptions are
    requested, public JobPosting details replace them. ``max_pages`` is a
    safety limit, not a truncation option.
    """

    ats = ATSType.BUILTIN
    fetch_escalate = True
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*",
    }

    def __init__(
        self, company_slug: str, *, timeout: float = 30.0,
        include_descriptions: bool = True, proxy: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES, request_delay: float = 2.0,
    ) -> None:
        super().__init__(company_slug, timeout=timeout,
                         include_descriptions=include_descriptions, proxy=proxy)
        if max_pages < 1 or request_delay < 0:
            raise ValueError("Built In requires positive max_pages and nonnegative request_delay")
        self.max_pages = max_pages
        self.request_delay = request_delay

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        async with self.make_fetcher(retries=4, retry_base_delay=5, max_retry_delay=120) as fetch:
            for page in range(1, self.max_pages + 1):
                if page > 1:
                    await asyncio.sleep(self.request_delay)
                text = await fetch.get_text(f"{API_ROOT}/jobs?page={page}")
                page_jobs = self._parse_listing(text)
                next_page = self._next_page(text, page)
                if not page_jobs:
                    raise ScraperError(f"Built In page {page} returned no recognized jobs")
                if any(job.company == "Unknown" for job in page_jobs):
                    raise ScraperError(f"Built In page {page} is missing employer metadata")
                new_jobs = [job for job in page_jobs if job.ats_id not in jobs]
                if not new_jobs:
                    raise ScraperError(f"Built In page {page} repeated previously seen jobs")
                jobs.update((job.ats_id or "", job) for job in new_jobs)
                if next_page is None:
                    break
            else:
                raise ScraperError(
                    f"Built In reached max_pages={self.max_pages} with more pages available; "
                    "refusing a partial result"
                )
            if self.include_descriptions:
                for job in jobs.values():
                    await asyncio.sleep(self.request_delay)
                    detail = await fetch.get_text(str(job.url))
                    job.description = self._detail_description(detail, job)
                    job.raw = {**(job.raw or {}), "description_kind": "full"}
        return list(jobs.values())

    def get_description(self, job: Job) -> str | None:
        if job.description and (job.raw or {}).get("description_kind") == "full":
            return job.description

        async def run() -> str:
            async with self.make_fetcher() as fetch:
                return self._detail_description(await fetch.get_text(str(job.url)), job)

        return self._run_sync(run())

    @staticmethod
    def _next_page(text: str, current: int) -> int | None:
        soup = BeautifulSoup(text, "html.parser")
        pagination = soup.select_one("ul.pagination")
        if pagination is None:
            raise ScraperError("Built In did not expose recognizable pagination")
        active = pagination.select_one("a.disabled")
        if (active and active.get_text(strip=True).isdigit()
                and int(active.get_text(strip=True)) != current):
            raise ScraperError("Built In returned a different page than requested")
        next_link = pagination.select_one('a[aria-label="Go to Next Page"]')
        if next_link is None:
            if any(link.get_text(strip=True).isdigit() and
                   int(link.get_text(strip=True)) > current
                   for link in pagination.find_all("a")):
                raise ScraperError("Built In has later pages but no recognized next link")
            return None
        target = urlsplit(urljoin(API_ROOT, str(next_link.get("href", ""))))
        values = parse_qs(target.query).get("page", [])
        if target.scheme != "https" or target.netloc != "builtin.com" or target.path != "/jobs":
            raise ScraperError("Built In returned an unexpected pagination URL")
        if values != [str(current + 1)]:
            raise ScraperError("Built In pagination did not advance to the next page")
        return current + 1

    def _parse_listing(self, text: str) -> list[Job]:
        soup = BeautifulSoup(text, "html.parser")
        items = None
        for node in _json_ld_nodes(soup):
            if node.get("@type") == "ItemList":
                items = node.get("itemListElement")
                break
        if not isinstance(items, list):
            raise ScraperError("Built In listing has no valid ItemList")
        jobs = [job for item in items if (job := self._parse_item(item))]
        if len(jobs) != len(items):
            raise ScraperError("Built In listing contains malformed job entries")
        cards: dict[str, Tag] = {}
        for card in soup.select('[data-id="job-card"]'):
            title = card.select_one('[data-id="job-card-title"]')
            if title:
                href = title.get("href")
                if isinstance(href, str):
                    cards[urljoin(API_ROOT, href).rstrip("/")] = card
        for job in jobs:
            matched_card = cards.get(str(job.url).rstrip("/"))
            if matched_card is None:
                continue
            company = matched_card.select_one('[data-id="company-title"]')
            if company and company.get_text(" ", strip=True):
                job.company = company.get_text(" ", strip=True)
            location = matched_card.select_one('[aria-label="Job locations"]')
            if location:
                tooltip = location.get("data-bs-title")
                if isinstance(tooltip, str) and tooltip:
                    job.location = "; ".join(BeautifulSoup(tooltip, "html.parser").stripped_strings) or None
                else:
                    job.location = location.get_text(" ", strip=True) or None
            else:
                icon = matched_card.select_one("i.fa-location-dot")
                if icon and icon.parent and icon.parent.parent:
                    job.location = icon.parent.parent.get_text(" ", strip=True) or None
            mode_icon = matched_card.select_one("i.fa-house-building")
            mode = None
            if mode_icon and mode_icon.parent and mode_icon.parent.parent:
                mode = mode_icon.parent.parent.get_text(" ", strip=True)
            if mode:
                job.is_remote = True if mode.lower() == "remote" else None
            salary_icon = matched_card.select_one("i.fa-sack-dollar")
            if salary_icon and salary_icon.parent and salary_icon.parent.parent:
                job.salary_summary = salary_icon.parent.parent.get_text(" ", strip=True) or None
            job.raw = {"description_kind": "summary", "workplace_type": mode}
            title = matched_card.select_one('[data-id="job-card-title"]')
            tracking_id = title.get("data-builtin-track-job-id") if title else None
            if isinstance(tracking_id, str) and tracking_id.isdigit() and tracking_id != job.ats_id:
                job.raw["source_job_id"] = tracking_id
        return jobs

    @staticmethod
    def _parse_item(item: object) -> Job | None:
        if not isinstance(item, dict):
            return None
        url, title = item.get("url"), item.get("name")
        if not isinstance(url, str) or not isinstance(title, str) or not title.strip():
            return None
        match = _JOB_URL_ID_RE.fullmatch(url.strip())
        if match is None:
            return None
        description = item.get("description")
        return Job(
            url=HttpUrl(url.strip()), title=html.unescape(title.strip()), company="Unknown",
            ats_type=ATSType.BUILTIN, ats_id=match.group("id"),
            description=html.unescape(description).strip()[:25000]
            if isinstance(description, str) else None,
            fetched_at=datetime.now(UTC), raw={"description_kind": "summary"},
        )

    @staticmethod
    def _detail_description(text: str, job: Job) -> str:
        for node in _json_ld_nodes(BeautifulSoup(text, "html.parser")):
            if node.get("@type") != "JobPosting":
                continue
            identifier = node.get("identifier")
            if isinstance(identifier, dict):
                identifier = identifier.get("value")
            url = node.get("url")
            if identifier is None and not isinstance(url, str):
                raise ScraperError("Built In detail is missing job identity")
            source_ids = {job.ats_id, (job.raw or {}).get("source_job_id")}
            if identifier is not None and str(identifier) not in source_ids:
                raise ScraperError("Built In detail returned a different job identity")
            if isinstance(url, str) and url.rstrip("/") != str(job.url).rstrip("/"):
                raise ScraperError("Built In detail URL does not match the listing")
            description = node.get("description")
            if isinstance(description, str) and description.strip():
                return html.unescape(description).strip()[:25000]
        raise ScraperError(f"Built In detail has no JobPosting description for {job.ats_id}")


def _json_ld_nodes(soup: BeautifulSoup) -> Iterator[dict[str, Any]]:
    for script in soup.find_all("script"):
        if html.unescape(str(script.get("type", ""))).lower() != "application/ld+json":
            continue
        try:
            payload = json.loads(script.get_text())
        except ValueError:
            continue
        nodes = payload.get("@graph", [payload]) if isinstance(payload, dict) else payload
        if isinstance(nodes, list):
            yield from (node for node in nodes if isinstance(node, dict))
