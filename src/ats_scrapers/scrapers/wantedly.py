"""Wantedly scraper.

Wantedly (wantedly.com) is a Japanese recruiting platform. Each employer has
a company page, ``https://www.wantedly.com/companies/{slug}``, and publishes
its openings there as "projects".

The site's own JSON API lists the projects of one company::

    GET https://www.wantedly.com/api/v1/projects?company_id={slug}&per_page=10&page={n}

- ``company_id`` takes the company's slug as written in its page address,
  capitals included. A slug the site does not know answers HTTP 500.
- ``per_page`` is capped at 10 and a listing ends after page 100, so one
  company can show at most 1,000 projects. ``_metadata`` gives the total and
  the number of pages. A company that announces more is refused: its list
  would be cut short without notice.
- Each project carries its full text (``description``), so no project page is
  read.
- The order of a listing is a ranking that moves between two requests, and
  no parameter fixes it. A project can therefore appear on two pages while
  another is skipped. The pages are read again, up to ``MAX_PASSES`` times,
  until every announced project has been seen.

Without ``company_id`` the same endpoint lists 1,000 projects out of some
190,000, which is why this scraper works company by company. The company
slugs come from the site's sitemap.

Employers republish one opening as several projects, each under a new
headline and often tagged with another prefecture, while the role label
(``looking_for``) and the text stay the same. Such projects are returned as
one job: the most recently published project, with the places of all of them
and the number of the others in ``raw["reposts"]``. ``collapse_reposts=False``
returns every project.

The API also returns the names of staff members of the employer. Nothing
about a person is read or stored.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

API_URL = "https://www.wantedly.com/api/v1/projects"
PROJECT_URL = "https://www.wantedly.com/projects/{id}"
COMPANY_URL = "https://www.wantedly.com/companies/{slug}"
PAGE_SIZE = 10
# The listing answers nothing after this page.
MAX_PAGES = 100
# Pages of one listing are read one after the other: the site answers 403
# to a client that asks for several at once for long.
PAGE_CONCURRENCY = 1
# How many times the pages of one listing are read to collect the projects
# its moving order skipped, and the share of the announced total below which
# the list is refused.
MAX_PASSES = 4
MIN_COVERAGE = 0.9
# Two projects of one company are the same opening when their role label and
# their text are the same. A text shorter than this is too little to tell.
MIN_REPOST_TEXT_CHARS = 200
# How many ids of the other projects of an opening are kept in ``raw``.
MAX_REPOST_IDS = 50
MAX_DESCRIPTION_CHARS = 25_000

# A company slug as it stands in the sitemap: ASCII letters, digits,
# underscore and hyphen. The slug is case-sensitive, so it is kept as given.
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_COMPANY_PATH_RE = re.compile(r"^/companies/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/.*)?$")


@ScraperRegistry.register(ATSType.WANTEDLY)
class WantedlyScraper(BaseScraper):
    """Scrape the projects of one Wantedly company."""

    ats = ATSType.WANTEDLY

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        # Without it the address serves the HTML page instead of JSON.
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
        collapse_reposts: bool = True,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = _parse_slug(company_slug)
        self.collapse_reposts = collapse_reposts
        self.company_name = (
            company_name.strip() if company_name and company_name.strip() else None
        )

    async def afetch(self) -> list[Job]:
        found: dict[str, dict[str, Any]] = {}
        total = 0
        async with self.make_fetcher() as fetch:
            semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)

            async def read(page: int) -> dict[str, Any]:
                async with semaphore:
                    return await self._page(fetch, page)

            # Projects are collected across the passes on purpose. One pass
            # of a moving ranking is never the whole list, so a pass cannot
            # stand alone. The price is small: a project closed between two
            # passes is still returned, a minute old at most, and is gone
            # at the next run.
            for _ in range(MAX_PASSES):
                first = await read(1)
                total, pages = _listing_size(first, self.company_slug)
                payloads = [first]
                if pages > 1:
                    payloads += await asyncio.gather(
                        *(read(page) for page in range(2, pages + 1))
                    )
                for payload in payloads:
                    for item in _items(payload, self.company_slug):
                        project_id = _project_id(item)
                        if project_id is None:
                            raise ScraperError(
                                f"Wantedly ({self.company_slug}) listed a "
                                "project without an id"
                            )
                        found.setdefault(project_id, item)
                if len(found) >= total:
                    break

        company_ids = {_company_id(item) for item in found.values()}
        if len(company_ids) > 1:
            # One listing, one employer. Several would mean the filter was
            # not applied and the site's general feed came back.
            raise ScraperError(
                f"Wantedly ({self.company_slug}) listed projects of "
                f"{len(company_ids)} companies"
            )
        # ``total`` is the one the last pass announced.
        if len(found) < math.ceil(total * MIN_COVERAGE):
            raise ScraperError(
                f"Wantedly ({self.company_slug}) announces {total} projects "
                f"and listed {len(found)} in {MAX_PASSES} passes"
            )
        fetched_at = datetime.now(UTC)
        return [
            self._parse_opening(projects, fetched_at)
            for projects in _openings(list(found.values()), self.collapse_reposts)
        ]

    async def _page(self, fetch: Fetcher, page: int) -> dict[str, Any]:
        payload = await fetch.get_json(
            API_URL,
            params={
                "company_id": self.company_slug,
                "per_page": PAGE_SIZE,
                "page": page,
            },
        )
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Wantedly ({self.company_slug}) page {page} was not an object"
            )
        return payload

    def _parse_opening(
        self, projects: list[dict[str, Any]], fetched_at: datetime
    ) -> Job:
        """One job for the projects of one opening, most recent first."""
        job = self._parse_job(projects[0], fetched_at)
        if len(projects) == 1:
            return job
        places = [place for item in projects if (place := _location(item))]
        job.location = "; ".join(dict.fromkeys(places)) or None
        others = [
            other for item in projects[1:] if (other := _project_id(item)) is not None
        ]
        job.raw = {
            **(job.raw or {}),
            "reposts": len(others),
            "repost_ids": others[:MAX_REPOST_IDS],
        }
        return job

    def _parse_job(self, item: dict[str, Any], fetched_at: datetime) -> Job:
        project_id = _project_id(item)
        title = _text(item.get("title"))
        if project_id is None or title is None:
            raise ScraperError(
                f"Wantedly ({self.company_slug}) listed a project without "
                "its id or title"
            )
        company = item.get("company")
        listed_name = _text(company.get("name")) if isinstance(company, dict) else None
        description = _text(item.get("description"), keep_lines=True)
        return Job(
            url=PROJECT_URL.format(id=project_id),
            title=title,
            company=self.company_name or listed_name or self.company_slug,
            ats_type=ATSType.WANTEDLY,
            ats_id=project_id,
            location=_location(item),
            description=(
                description[:MAX_DESCRIPTION_CHARS]
                if description and self.include_descriptions
                else None
            ),
            posted_at=_parse_datetime(item.get("published_at")),
            fetched_at=fetched_at,
            raw={"company_slug": self.company_slug},
        )


def _parse_slug(value: str) -> str:
    """A company slug, or the slug named by a Wantedly company address."""
    cleaned = value.strip()
    if "/" in cleaned:
        from urllib.parse import urlparse

        parsed = urlparse(cleaned if "//" in cleaned else f"https://{cleaned}")
        match = _COMPANY_PATH_RE.match(parsed.path)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in {"www.wantedly.com", "wantedly.com"}
            or match is None
        ):
            raise ScraperError(
                f"WantedlyScraper: {value!r} is not a Wantedly company address"
            )
        cleaned = match.group(1)
    if not _SLUG_RE.fullmatch(cleaned):
        raise ScraperError(f"WantedlyScraper: invalid company slug {value!r}")
    return cleaned


def _openings(
    projects: list[dict[str, Any]], collapse_reposts: bool
) -> list[list[dict[str, Any]]]:
    """The projects grouped by opening, each group most recent first.

    Groups keep the order in which their first project was listed."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for index, item in enumerate(projects):
        key = _repost_key(item) if collapse_reposts else None
        groups.setdefault(key or ("", f"#{index}"), []).append(item)
    return [sorted(group, key=_recency, reverse=True) for group in groups.values()]


def _repost_key(item: dict[str, Any]) -> tuple[str, str] | None:
    """What two projects of one opening share: the role label and the text.

    The text is compared without its whitespace: the same text pasted again
    often differs only in its line breaks."""
    description = item.get("description")
    text = "".join(description.split()) if isinstance(description, str) else ""
    if len(text) < MIN_REPOST_TEXT_CHARS:
        return None
    return (_text(item.get("looking_for")) or "", text)


def _recency(item: dict[str, Any]) -> tuple[datetime, int]:
    published = _parse_datetime(item.get("published_at"))
    project_id = _project_id(item)
    return (
        published or datetime.min.replace(tzinfo=UTC),
        int(project_id) if project_id is not None else 0,
    )


def _listing_size(payload: dict[str, Any], slug: str) -> tuple[int, int]:
    """The announced number of projects and the number of pages to read."""
    metadata = payload.get("_metadata")
    total = _count(metadata.get("total_objects")) if isinstance(metadata, dict) else None
    pages = _count(metadata.get("total_pages")) if isinstance(metadata, dict) else None
    if total is None or pages is None:
        raise ScraperError(f"Wantedly ({slug}) listing gave no total")
    if pages > MAX_PAGES:
        # The site shows no page past the cap: the list would be cut short
        # without notice.
        raise ScraperError(
            f"Wantedly ({slug}) announces {total} projects on {pages} pages; "
            f"only {MAX_PAGES} pages can be read"
        )
    return total, pages


def _items(payload: dict[str, Any], slug: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ScraperError(f"Wantedly ({slug}) listing gave no project list")
    return list(data)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _project_id(item: dict[str, Any]) -> str | None:
    value = item.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    return None


def _company_id(item: dict[str, Any]) -> str | None:
    company = item.get("company")
    if not isinstance(company, dict):
        return None
    value = company.get("id")
    return None if value is None or isinstance(value, bool) else str(value)


def _text(value: object, *, keep_lines: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    if keep_lines:
        lines = [" ".join(line.split()) for line in value.splitlines()]
        cleaned = "\n".join(lines).strip()
        return re.sub(r"\n{3,}", "\n\n", cleaned) or None
    return " ".join(value.split()) or None


def _location(item: dict[str, Any]) -> str | None:
    """The place as the employer wrote it: the address, then its free note."""
    parts = [
        text
        for key in ("location", "location_suffix")
        if (text := _text(item.get(key)))
    ]
    return " ".join(dict.fromkeys(parts)) or None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
