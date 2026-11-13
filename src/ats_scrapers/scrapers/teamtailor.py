"""Teamtailor scraper.

Teamtailor's main API requires authentication (`api.teamtailor.com/v1/...`
returns 406 without an API key), but every public careers site exposes a
free RSS feed at `/jobs.rss` with all the structured fields we need:

    GET https://{slug}.teamtailor.com/jobs.rss

Each `<item>` carries title, link, pubDate, guid, custom `tt:` location
(city, country, name), `tt:department`, `tt:role`, and an HTML description.

The feed returns at most 100 items per request. A tenant with more open
jobs is read page by page with ``?offset=100``, ``?offset=200``, ... until
a page comes back short or brings no posting that was not already seen.
The first request carries no parameter, so a tenant with fewer than 100
jobs is still a single-request scrape.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, ClassVar
from xml.etree import ElementTree as ET

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

RSS_TEMPLATE = "https://{slug}.teamtailor.com/jobs.rss"
PAGE_SIZE = 100  # items the feed returns per request
MAX_PAGES = 200  # 20,000 postings: past this the walk raises instead of going on
TT_NS = {"tt": "https://teamtailor.com/locations"}

# URL form: `https://{slug}.teamtailor.com/jobs/{numeric_id}-{slug-title}`
_URL_ID_RE = re.compile(r"/jobs/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.TEAMTAILOR)
class TeamtailorScraper(BaseScraper):
    """Teamtailor scraper — `company_slug` is the tenant subdomain."""

    ats = ATSType.TEAMTAILOR

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml, text/xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = require_host_label(
            company_slug, provider="TeamtailorScraper"
        )

    async def afetch(self) -> list[Job]:
        url = RSS_TEMPLATE.format(slug=self.company_slug)
        jobs: list[Job] = []
        seen: set[str] = set()
        async with self.make_fetcher() as fetch:
            for page in range(MAX_PAGES):
                # The first request is the plain feed; later pages add the
                # offset. A failed later page raises: a partial list must
                # not pass for the tenant's full catalogue.
                params = {"offset": str(page * PAGE_SIZE)} if page else None
                xml_text = await fetch.get_text(url, params=params)
                items, added = self._absorb(xml_text, jobs, seen)
                if items < PAGE_SIZE or added == 0:
                    # A short page is the last one. A full page with nothing
                    # new means the feed ignored the offset or the walk has
                    # wrapped around: either way there is nothing more to read.
                    break
            else:
                # Every page up to the guard was full: the list is cut, and
                # a cut list must not pass for the tenant's full catalogue.
                raise ScraperError(
                    f"Teamtailor ({self.company_slug}) still returned full "
                    f"pages after {len(jobs)} postings (page guard "
                    f"{MAX_PAGES}); refusing to return a partial list"
                )
        return jobs

    def _absorb(
        self, xml_text: str, jobs: list[Job], seen: set[str],
    ) -> tuple[int, int]:
        """Add one feed page to ``jobs``.

        Returns ``(items, added)``: how many ``<item>`` elements the page
        held, and how many of them were postings not seen before.
        """
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

        items = 0
        added = 0
        for item in root.iter("item"):
            items += 1
            job = self._parse_item(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
            added += 1
        return items, added

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
            fetched_at=datetime.now(UTC),
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
    """Compose 'City, Country' for every `<tt:location>` child.

    Multi-office postings list one child per office; they are joined
    with '; ' in feed order, duplicates dropped."""
    rendered: list[str] = []
    for loc in item.findall("tt:locations/tt:location", TT_NS):
        parts: list[str] = []
        for tag in ("city", "country"):
            value = (loc.findtext(f"tt:{tag}", namespaces=TT_NS) or "").strip()
            if value:
                parts.append(value)
        if parts:
            rendered.append(", ".join(parts))
            continue
        name = (loc.findtext("tt:name", namespaces=TT_NS) or "").strip()
        if name:
            rendered.append(name)
    return "; ".join(dict.fromkeys(rendered)) or None


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
