"""Manatal career pages.

A Manatal customer publishes its jobs at
``https://www.careers-page.com/{slug}``, backed by a public JSON API::

    GET /api/v1.0/c/{slug}/                  -> the organisation
    GET /api/v1.0/c/{slug}/jobs/?page_size=20&page={n}  -> the jobs, with their text

The API refuses with 403 for a while when asked too fast, so pages are read
one at a time with a pause, and a refusal is waited out.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._htmltree import parse_html
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher, FetchResponse

logger = logging.getLogger(__name__)

SITE = "https://www.careers-page.com"
API = f"{SITE}/api/v1.0/c"
# The API serves 10 jobs per page by default and 20 at most.
PAGE_SIZE = 20
PAGE_PAUSE_SECONDS = 0.5
REFUSAL_WAIT_SECONDS = 45.0
MAX_REFUSALS = 4
MAX_PAGES = 2_000
MISSING_ALLOWED = 2

_SLUG_RE = re.compile(r"-?[A-Za-z0-9][A-Za-z0-9._~-]*")


@ScraperRegistry.register(ATSType.MANATAL)
class ManatalScraper(BaseScraper):
    """One Manatal career page.

    ``company_slug`` is the page slug (``acme``) or its URL
    (``https://www.careers-page.com/acme``).
    """

    ats = ATSType.MANATAL

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        super().__init__(
            _slug(company_slug),
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_name = (company_name or "").strip() or None
        self.board_url = f"{SITE}/{quote(self.company_slug, safe='')}"
        self._api_url = f"{API}/{quote(self.company_slug, safe='')}"

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            company = self.company_name or await self._organisation_name(fetch)
            jobs: dict[str, Job] = {}
            unusable = 0
            announced = 0
            url: str | None = f"{self._api_url}/jobs/?page_size={PAGE_SIZE}"
            for _ in range(MAX_PAGES):
                if url is None:
                    break
                body = (await self._get(fetch, url)).json()
                results = body.get("results") if isinstance(body, dict) else None
                if not isinstance(results, list):
                    raise ScraperError(
                        f"Manatal ({self.company_slug}) returned no list of jobs"
                    )
                count = body.get("count")
                if isinstance(count, int) and not isinstance(count, bool):
                    announced = max(announced, count)
                for item in results:
                    job = self._parse_job(item, company) if isinstance(item, dict) else None
                    if job is None:
                        unusable += 1
                    elif job.ats_id not in jobs:
                        jobs[job.ats_id] = job  # type: ignore[index]
                url = _next_page(body.get("next"))
                if url is not None:
                    await asyncio.sleep(PAGE_PAUSE_SECONDS)
            else:
                raise ScraperError(
                    f"Manatal ({self.company_slug}) has more than {MAX_PAGES} pages"
                )
        # Jobs open and close while the pages are read; more than a few
        # missing means pages shifted and jobs were skipped.
        missing = announced - len(jobs) - unusable
        if missing > max(MISSING_ALLOWED, announced // 20):
            raise ScraperError(
                f"Manatal ({self.company_slug}) returned {len(jobs)} of "
                f"{announced} jobs"
            )
        return list(jobs.values())

    async def _organisation_name(self, fetch: Fetcher) -> str:
        body = (await self._get(fetch, f"{self._api_url}/")).json()
        name = body.get("name") if isinstance(body, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else self.company_slug

    async def _get(self, fetch: Fetcher, url: str) -> FetchResponse:
        """GET that waits out the API's temporary 403 refusals."""
        for attempt in range(1, MAX_REFUSALS + 1):
            response = await fetch.request("GET", url, handled={403})
            if response.status_code != 403:
                return response
            if attempt < MAX_REFUSALS:
                logger.warning(
                    "Manatal (%s) refused a request; waiting %.0fs",
                    self.company_slug,
                    REFUSAL_WAIT_SECONDS,
                )
                await asyncio.sleep(REFUSAL_WAIT_SECONDS)
        raise ScraperError(
            f"Manatal ({self.company_slug}) kept refusing requests ({url})"
        )

    def _parse_job(self, item: dict[str, Any], company: str) -> Job | None:
        job_hash = _text(item.get("hash"))
        title = _text(item.get("position_name"))
        if not job_hash or not title:
            return None
        place = _text(item.get("location_display")) or ", ".join(
            dict.fromkeys(
                part
                for key in ("city", "state", "country")
                if (part := _text(item.get(key)))
            )
        )
        description = None
        if self.include_descriptions and (body := _text(item.get("description"))):
            description = parse_html(body).lines().strip()[:25_000] or None
        return Job(
            url=HttpUrl(f"{self.board_url}/job/{quote(job_hash, safe='')}"),
            title=title,
            company=company,
            ats_type=ATSType.MANATAL,
            ats_id=job_hash,
            location=place or None,
            description=description,
            requisition_id=str(item["id"]) if isinstance(item.get("id"), int) else None,
            fetched_at=datetime.now(tz=UTC),
        )


def _slug(company_slug: str) -> str:
    slug = company_slug.strip()
    if "://" in slug:
        parsed = urlparse(slug)
        if (parsed.hostname or "").lower().removeprefix("www.") != "careers-page.com":
            raise ScraperError(
                f"ManatalScraper needs a careers-page.com page, got {company_slug!r}"
            )
        segments = [part for part in parsed.path.split("/") if part]
        slug = segments[0] if segments else ""
    if not _SLUG_RE.fullmatch(slug):
        raise ScraperError(
            f"ManatalScraper needs a careers-page.com page slug, got {company_slug!r}"
        )
    return slug


def _next_page(value: object) -> str | None:
    """The next page of the same API, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if (parsed.hostname or "").lower() != "www.careers-page.com":
        return None
    return parsed._replace(scheme="https").geturl()


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
