"""Workable XML job feed: every posting published on Jobs by Workable.

Workable offers one XML file for bulk use, holding the jobs of its customer
accounts that are approved and published on Jobs by Workable. Its address is
shared on request: ask Workable support for access to the jobs XML feed, then
pass the address they give you as ``feed_url`` or set
``ATS_SCRAPERS_WORKABLE_FEED_URL``.

The file is large (hundreds of MB), takes no filter parameters and is
rebuilt every hour. Workable asks consumers to download it at most once per
hour.

Each ``<job>`` holds CDATA fields: ``title``, ``date``, ``referencenumber``
(the job's shortcode), ``url`` (``https://apply.workable.com/j/{shortcode}``),
``company``, ``city``, ``state``, ``country`` (ISO 3166-1 alpha-2),
``remote``, ``postalcode``, ``description`` (HTML, requirements and
benefits included), ``education``, ``jobtype``, ``category``,
``experience`` and ``website``.

A posting with several locations appears once per location, not always
next to each other. The scraper groups them into one job per shortcode.
``url`` is kept exactly as published so that Workable can attribute
candidates to their source.

The file is streamed to a temporary file and read twice with
``iterparse`` (locations first, then one job per shortcode), so memory
stays flat whatever its size.

Single-source scraper: ``company_slug`` is ignored. Not in the scraper
registry, where ``workable`` is :class:`WorkableScraper` (one employer's
board).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import proxy_url_from_env
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._job_classification import classification_label, education_level
from ats_scrapers.scrapers._job_places import distinct_places, job_location
from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.workable import _EMPLOYMENT_TYPE_PATTERNS

FEED_URL_ENV = "ATS_SCRAPERS_WORKABLE_FEED_URL"
MAX_DESCRIPTION_LEN = 25_000
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY = 10.0
# A longer Retry-After fails the run instead of holding it.
MAX_RETRY_AFTER = 600.0
_CHUNK = 1 << 20


@dataclass
class _Locations:
    places: dict[tuple[str, str, str], str] = field(default_factory=dict)
    countries: set[str] = field(default_factory=set)
    remote: bool = False
    posted_at: datetime | None = None


class WorkableFeedScraper(BaseScraper):
    """Every job of the Workable XML feed, one per shortcode."""

    ats = ATSType.WORKABLE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
    }

    def __init__(
        self,
        company_slug: str = "workable",
        *,
        timeout: float = 120.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        feed_url: str | None = None,
        feed_path: str | Path | None = None,
    ) -> None:
        """``feed_url`` defaults to ``ATS_SCRAPERS_WORKABLE_FEED_URL``.

        ``feed_path`` reads a feed already on disk instead of downloading it.
        """
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.feed_url = feed_url or os.environ.get(FEED_URL_ENV) or None
        self.feed_path = Path(feed_path) if feed_path is not None else None

    async def afetch(self) -> list[Job]:
        return [job async for job in self.fetch_stream()]

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Yield one job per shortcode while the feed is read."""
        if self.feed_path is not None:
            async for job in self._parse(self.feed_path):
                yield job
            return
        if not self.feed_url:
            raise ScraperError(
                "Workable feed address not set: ask Workable support for access "
                f"to the jobs XML feed, then pass feed_url or set {FEED_URL_ENV}"
            )
        fd, name = tempfile.mkstemp(prefix="workable-feed-", suffix=".xml")
        os.close(fd)
        path = Path(name)
        try:
            await self._download(path)
            async for job in self._parse(path):
                yield job
        finally:
            path.unlink(missing_ok=True)

    async def _parse(self, path: Path) -> AsyncGenerator[Job, None]:
        locations = await asyncio.to_thread(_collect_locations, path)
        if not locations:
            raise ScraperError("Workable feed holds no jobs")
        for count, job in enumerate(
            parse_jobs(path, locations, include_description=self.include_descriptions),
            start=1,
        ):
            yield job
            if count % 1000 == 0:
                # Parsing is synchronous: let other tasks run now and then.
                await asyncio.sleep(0)

    async def _download(self, path: Path) -> None:
        proxy = self.proxy if self.proxy is not None else proxy_url_from_env()
        error = "no attempt"
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            delay = RETRY_DELAY * attempt
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers=self.default_headers,
                    proxy=proxy,
                ) as client, client.stream("GET", self.feed_url) as response:
                    if response.status_code >= 500 or response.status_code == 429:
                        error = f"HTTP {response.status_code}"
                        retry_after = response.headers.get("retry-after", "")
                        if retry_after.isdigit():
                            if float(retry_after) > MAX_RETRY_AFTER:
                                raise ScraperError(
                                    f"Workable feed returned {error} with "
                                    f"Retry-After {retry_after}s"
                                )
                            delay = float(retry_after)
                    elif response.status_code != 200:
                        raise ScraperError(
                            f"Workable feed returned HTTP {response.status_code}"
                        )
                    else:
                        # Content-Length counts compressed bytes when the feed is
                        # served compressed; a truncated compressed file fails
                        # the XML parse instead.
                        expected = None
                        if "content-encoding" not in response.headers:
                            expected = response.headers.get("content-length")
                        written = 0
                        with path.open("wb") as fh:
                            async for chunk in response.aiter_bytes(_CHUNK):
                                fh.write(chunk)
                                written += len(chunk)
                        if expected and expected.isdigit() and written != int(expected):
                            error = f"download stopped at {written} of {expected} bytes"
                        else:
                            return
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
            if attempt < DOWNLOAD_ATTEMPTS:
                await asyncio.sleep(delay)
        raise ScraperError(f"Workable feed download failed: {error}")


def _iter_entries(path: Path) -> Iterator[dict[str, str]]:
    """Each ``<job>`` as a dict of stripped field texts."""
    try:
        context = ET.iterparse(path, events=("start", "end"))
        _, root = next(context)
        for event, elem in context:
            if event == "end" and elem.tag == "job":
                yield {child.tag: (child.text or "").strip() for child in elem}
                root.clear()
    except ET.ParseError as exc:
        # A truncated or damaged file: never publish a partial feed.
        raise ScraperError(f"Workable feed is not valid XML: {exc}") from exc
    except StopIteration as exc:
        raise ScraperError("Workable feed is empty") from exc


def _collect_locations(path: Path) -> dict[str, _Locations]:
    """First pass: every location of every shortcode."""
    locations: dict[str, _Locations] = {}
    for entry in _iter_entries(path):
        shortcode = entry.get("referencenumber", "")
        if not shortcode:
            continue
        found = locations.setdefault(shortcode, _Locations())
        place = (entry.get("city", ""), entry.get("state", ""), entry.get("country", ""))
        if any(place):
            found.places[place] = ", ".join(part for part in place if part)
        if entry.get("country"):
            found.countries.add(entry["country"].upper())
        found.remote = found.remote or entry.get("remote", "").lower() == "true"
        posted_at = _parse_date(entry.get("date"))
        if posted_at and (found.posted_at is None or posted_at < found.posted_at):
            found.posted_at = posted_at
    return locations


def parse_jobs(
    path: Path,
    locations: dict[str, _Locations] | None = None,
    *,
    include_description: bool = True,
) -> Iterator[Job]:
    """One job per shortcode from a feed file on disk, in feed order."""
    if locations is None:
        locations = _collect_locations(path)
    done: set[str] = set()
    for entry in _iter_entries(path):
        shortcode = entry.get("referencenumber", "")
        if not shortcode or shortcode in done:
            continue
        done.add(shortcode)
        job = _job(entry, locations[shortcode], include_description=include_description)
        if job is not None:
            yield job


def _job(entry: dict[str, str], found: _Locations, *, include_description: bool) -> Job | None:
    title, url = entry.get("title"), entry.get("url")
    if not title or not url:
        return None
    commitment = entry.get("jobtype") or None
    employment_type = None
    if commitment:
        norm = commitment.lower()
        employment_type = next(
            (mapped for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items() if needle in norm),
            None,
        )
    raw: dict[str, Any] = {
        key: entry[key] for key in ("education", "experience", "website") if entry.get(key)
    }
    description = entry.get("description") if include_description else None
    return Job(
        url=url,
        title=title,
        company=entry.get("company") or "Unknown",
        ats_type=ATSType.WORKABLE,
        ats_id=entry["referencenumber"],
        location=" | ".join(dict.fromkeys(found.places.values())) or None,
        locations=distinct_places(
            job_location(label, city=city, region=state, country_code=country)
            for (city, state, country), label in found.places.items()
        ),
        country_iso=next(iter(found.countries)) if len(found.countries) == 1 else None,
        is_remote=found.remote,
        department=entry.get("category") or None,
        job_category=classification_label(entry.get("category")),
        education_requirements=education_level(entry.get("education")),
        employment_type=employment_type,
        commitment=commitment,
        description=description[:MAX_DESCRIPTION_LEN] if description else None,
        posted_at=found.posted_at,
        fetched_at=datetime.now(UTC),
        raw=raw or None,
    )


def _parse_date(value: str | None) -> datetime | None:
    # RFC 2822 dates ("Wed, 28 Jul 2021 10:24:17 UTC"), read without the
    # process locale.
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
