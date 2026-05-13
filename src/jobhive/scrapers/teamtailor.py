"""Teamtailor scraper.

Teamtailor's main API requires authentication (`api.teamtailor.com/v1/...`
returns 406 without an API key), but every public careers site exposes a
free RSS feed at `/jobs.rss` with all the structured fields we need:

    GET https://{slug}.teamtailor.com/jobs.rss

Each `<item>` carries title, link, pubDate, guid, custom `tt:` location
(city, country, name), `tt:department`, `tt:role`, and an HTML description.

This is a single-request scrape — Teamtailor's RSS includes every open job,
no pagination. Tenants with hundreds of jobs return ~200KB of XML.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

RSS_TEMPLATE = "https://{slug}.teamtailor.com/jobs.rss"
TT_NS = {"tt": "https://teamtailor.com/locations"}

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# URL form: `https://{slug}.teamtailor.com/jobs/{numeric_id}-{slug-title}`
_URL_ID_RE = re.compile(r"/jobs/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.TEAMTAILOR)
class TeamtailorScraper(BaseScraper):
    """Teamtailor scraper — `company_slug` is the tenant subdomain."""

    ats = ATSType.TEAMTAILOR

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            xml_text = await self._fetch_rss(client)
        return self._parse_rss(xml_text)

    async def _fetch_rss(self, client: httpx.AsyncClient) -> str:
        url = RSS_TEMPLATE.format(slug=self.company_slug)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/rss+xml, text/xml",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Teamtailor fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Teamtailor tenant not found: {self.company_slug}"
                )
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Teamtailor ({self.company_slug}) returned "
                        f"{response.status_code} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Teamtailor ({self.company_slug}) returned {response.status_code}"
            )
        raise ScraperError(f"Teamtailor ({self.company_slug}) exhausted retries")

    def _parse_rss(self, xml_text: str) -> list[Job]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ScraperError(
                f"Teamtailor ({self.company_slug}) returned malformed RSS: {exc}"
            ) from exc
        # The response parses as XML but isn't an RSS feed (e.g. an HTML
        # error page wrapped in <html>...). Treat as malformed so callers
        # don't get an empty list and assume "tenant has no jobs".
        if root.tag.lower() != "rss" and root.find(".//channel") is None:
            raise ScraperError(
                f"Teamtailor ({self.company_slug}) returned malformed RSS: "
                f"root element <{root.tag}> is not <rss>"
            )

        jobs: list[Job] = []
        seen: set[str] = set()
        for item in root.iter("item"):
            job = self._parse_item(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _parse_item(self, item: ET.Element) -> Job | None:
        link = (item.findtext("link") or "").strip()
        if not link:
            return None
        # Prefer the numeric ID from the URL — it's stable, public, and
        # shorter than the GUID UUID. Fall back to GUID if the URL lacks one.
        ats_id = ""
        if (m := _URL_ID_RE.search(link)):
            ats_id = m.group(1)
        if not ats_id:
            ats_id = (item.findtext("guid") or "").strip()
        if not ats_id:
            return None
        title = (item.findtext("title") or "").strip() or "Untitled"
        description = self._strip_description(item.findtext("description"))
        dept = (item.findtext("tt:department", namespaces=TT_NS) or "").strip()
        return Job(
            url=link,
            title=title,
            company=self.company_slug,
            ats_type=ATSType.TEAMTAILOR,
            ats_id=ats_id,
            location=_format_location(item),
            is_remote=_extract_remote(item),
            department=dept or None,
            posted_at=_parse_pubdate(item.findtext("pubDate")),
            description=description,
            fetched_at=datetime.now(),
        )

    def _strip_description(self, raw: str | None) -> str | None:
        if not raw:
            return None
        text = html.unescape(raw)
        text = _TAG_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        return text[:25_000]


def _format_location(item: ET.Element) -> str | None:
    """Compose 'City, Country' from the first `<tt:location>` child."""
    loc = item.find("tt:locations/tt:location", TT_NS)
    if loc is None:
        return None
    parts: list[str] = []
    for tag in ("city", "country"):
        value = (loc.findtext(f"tt:{tag}", namespaces=TT_NS) or "").strip()
        if value:
            parts.append(value)
    if parts:
        return ", ".join(parts)
    name = (loc.findtext("tt:name", namespaces=TT_NS) or "").strip()
    return name or None


def _extract_remote(item: ET.Element) -> bool | None:
    """Teamtailor's `<remoteStatus>` is one of: 'fully', 'temporary',
    'hybrid', 'none'. Map the unambiguous extremes; treat hybrid/temporary
    as None ("we don't know")."""
    status = (item.findtext("remoteStatus") or "").strip().lower()
    if not status:
        return None
    if status == "fully":
        return True
    if status == "none":
        return False
    return None


def _parse_pubdate(value: str | None) -> datetime | None:
    """RFC 2822 dates from RSS, e.g. 'Fri, 20 Mar 2026 09:30:04 +0100'."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
