"""Greeting (그리팅) career sites.

An employer on Greeting publishes under
``https://{tenant}.career.greetinghr.com``. The site is a Next.js app whose
pages carry their data in ``__NEXT_DATA__``::

    GET /            -> the workspace (employer) and the list of openings
    GET /o/{id}      -> one opening, with its description

The list has no descriptions, so they take one request per opening.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry
from ats_scrapers.scrapers.ninehire import _location_geography

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

HOST_SUFFIX = ".career.greetinghr.com"
DETAIL_CONCURRENCY = 4

_TENANT_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "FULL_TIME_WORKER": "FULL_TIME",
    "CONTRACT_WORKER": "CONTRACT",
    "INTERN_WORKER": "INTERN",
    "PART_TIME_WORKER": "PART_TIME",
    "FREE_LANCER": "CONTRACT",
}


@ScraperRegistry.register(ATSType.GREETING)
class GreetingScraper(BaseScraper):
    """One Greeting tenant.

    ``company_slug`` is the tenant label (``kurly``), its host or a URL of
    its career site.
    """

    ats = ATSType.GREETING

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        tenant = _tenant(company_slug)
        super().__init__(
            tenant,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.base_url = f"https://{tenant}{HOST_SUFFIX}"
        self.company_name = (company_name or "").strip() or None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            queries = _queries(await fetch.get_text(f"{self.base_url}/"), self.base_url)
            openings = _query(queries, "openings")
            if not isinstance(openings, list):
                raise ScraperError(
                    f"Greeting ({self.company_slug}) page omitted the list of openings"
                )
            company = self.company_name or _workspace_name(queries) or self.company_slug
            jobs = [
                job
                for item in openings
                if isinstance(item, dict)
                and (job := self._parse_opening(item, company)) is not None
            ]
            if self.include_descriptions and jobs:
                await self._add_descriptions(fetch, jobs)
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        return self._run_sync(self._description(str(job.url)))

    async def _description(self, url: str) -> str | None:
        async with self.make_fetcher() as fetch:
            return await _opening_description(fetch, url)

    async def _add_descriptions(self, fetch: Fetcher, jobs: list[Job]) -> None:
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def fill(job: Job) -> None:
            async with semaphore:
                try:
                    description = await _opening_description(fetch, str(job.url))
                except ScraperError as exc:
                    # The opening stays, without its text.
                    logger.warning("Greeting opening %s could not be read: %s", job.url, exc)
                    return
            if description:
                job.description = description[:25_000]

        await asyncio.gather(*(fill(job) for job in jobs))

    def _parse_opening(self, item: dict[str, Any], company: str) -> Job | None:
        ats_id = str(item.get("openingId") or "").strip()
        title = str(item.get("title") or "").strip()
        if not ats_id or not title:
            return None
        positions = _positions(item)
        places = [p for p in (_place(pos) for pos in positions) if p]
        names = list(dict.fromkeys(place["name"] for place in places))
        country_iso, region = _location_geography(names) if names else (None, None)
        first_geo = next((p for p in places if p["lat"] is not None), None)
        employment = [
            value
            for pos in positions
            if isinstance(value := _nested(pos, "jobPositionEmployment", "employmentType"), str)
        ]
        occupations = list(dict.fromkeys(
            value
            for pos in positions
            if isinstance(value := _nested(pos, "workspaceOccupation", "occupation"), str)
            and value.strip()
        ))
        careers = list(dict.fromkeys(
            value
            for pos in positions
            if isinstance(value := _nested(pos, "jobPositionCareer", "careerType"), str)
        ))
        raw: dict[str, Any] = {}
        if careers:
            raw["career_types"] = careers
        if employment:
            raw["employment_types"] = list(dict.fromkeys(employment))
        return Job(
            url=HttpUrl(f"{self.base_url}/o/{ats_id}"),
            title=title,
            company=company,
            ats_type=ATSType.GREETING,
            ats_id=ats_id,
            location="; ".join(names) or None,
            country_iso=country_iso,
            region=region,
            lat=first_geo["lat"] if first_geo else None,
            lon=first_geo["lon"] if first_geo else None,
            is_remote=True if any(p["remote"] for p in places) else None,
            department="; ".join(occupations) or None,
            employment_type=next(
                (_EMPLOYMENT_TYPES[v] for v in employment if v in _EMPLOYMENT_TYPES), None
            ),
            posted_at=_parse_time(item.get("openDate")),
            application_deadline=_parse_time(item.get("dueDate")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


async def _opening_description(fetch: Fetcher, url: str) -> str | None:
    queries = _queries(await fetch.get_text(url), url)
    opening = _query(queries, "getOpeningById")
    data = opening.get("data") if isinstance(opening, dict) else None
    info = data.get("openingsInfo") if isinstance(data, dict) else None
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            info = None
    detail = info.get("detail") if isinstance(info, dict) else None
    return detail.strip() or None if isinstance(detail, str) else None


def _tenant(company_slug: str) -> str:
    slug = company_slug.strip().lower()
    if "://" in slug:
        slug = slug.split("://", 1)[1]
    host = slug.split("/", 1)[0]
    tenant = host.removesuffix(HOST_SUFFIX)
    if not _TENANT_RE.fullmatch(tenant):
        raise ScraperError(
            f"GreetingScraper needs a tenant of *{HOST_SUFFIX}, got {company_slug!r}"
        )
    return tenant


def _queries(page: str, url: str) -> list[dict[str, Any]]:
    """The data queries a Greeting page was rendered from."""
    match = _NEXT_DATA_RE.search(page)
    if not match:
        raise ScraperError(f"Greeting page {url} carries no page data")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise ScraperError(f"Greeting page {url} carries unreadable page data") from exc
    try:
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as exc:
        if isinstance(data, dict) and data.get("page") == "/404":
            raise CompanyNotFoundError(f"Greeting page {url} does not exist") from exc
        raise ScraperError(f"Greeting page {url} carries no data queries") from exc
    return [q for q in queries if isinstance(q, dict)] if isinstance(queries, list) else []


def _query(queries: list[dict[str, Any]], name: str) -> Any:
    """The data of the query whose key names ``name``."""
    for query in queries:
        key = query.get("queryKey")
        if isinstance(key, list) and name in key:
            state = query.get("state")
            return state.get("data") if isinstance(state, dict) else None
    return None


def _workspace_name(queries: list[dict[str, Any]]) -> str | None:
    base = _query(queries, "getCareerBaseInfo")
    name = _nested(base, "workspace", "name")
    return name.strip() or None if isinstance(name, str) else None


def _nested(value: object, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _positions(item: dict[str, Any]) -> list[dict[str, Any]]:
    positions = _nested(item, "openingJobPosition", "openingJobPositions")
    return [p for p in positions if isinstance(p, dict)] if isinstance(positions, list) else []


def _place(position: dict[str, Any]) -> dict[str, Any] | None:
    place = position.get("workspacePlace")
    if not isinstance(place, dict):
        return None
    name = next(
        (
            value.strip()
            for value in (place.get("place"), place.get("location"))
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    if not name:
        return None
    geo = place.get("geo") if isinstance(place.get("geo"), dict) else {}
    return {
        "name": name,
        "lat": _coordinate(geo.get("lat")),
        "lon": _coordinate(geo.get("lng")),
        "remote": place.get("workFromHome") is True,
    }


def _coordinate(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
