"""SAP SuccessFactors careers scraper.

Used by Procter & Gamble, Pfizer, Daimler, Schindler, and many others.

SuccessFactors Recruiting Marketing instances expose a public RSS 2.0 feed
at the canonical (typo-included, undocumented but stable) path:

    GET https://{recruiting-marketing-host}/sitemal.xml

Yes, the path is ``sitemal.xml`` (one ``p`` short of ``sitemap``) — that's
SAP's actual URL. Each ``<item>`` carries the job title (with location often
appended in parens), an HTML-escaped ``description``, ``link``, and
``pubDate``. The Google Merchant namespace adds ``g:id``, ``g:location``,
etc. on some tenants.

There is also a server-side XML feed at ``career{N}.successfactors.com/career?company={ID}&...``
that requires a tenant-specific ``company`` ID and picklist filters — we
prefer the simpler RSS path here. Pass the recruiting-marketing host as
``company_slug``.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_TAG_RE = re.compile(r"<[^>]+>")
# Job titles are often `"Title (City, State, Country)"` — extract location
# from the trailing parens when no other source is available.
_TITLE_LOCATION_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<loc>[^()]+)\)\s*$")
# Google Merchant namespace
_GOOGLE_NS = {"g": "http://base.google.com/ns/1.0"}

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "apprentice": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
}


@ScraperRegistry.register(ATSType.SUCCESSFACTORS)
class SuccessFactorsScraper(BaseScraper):
    """SAP SuccessFactors scraper. ``company_slug`` is the recruiting-marketing
    host (e.g. ``"job.schindler.com"`` → ``https://job.schindler.com/sitemal.xml``).

    Bare slugs are also accepted (``"schindler"`` → assumes ``job.schindler.com``).
    """

    ats = ATSType.SUCCESSFACTORS

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        feed_url = self._resolve_feed_url()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            xml_text = await self._fetch_feed(client, feed_url)
        return self._parse_feed(xml_text)

    def _resolve_feed_url(self) -> str:
        slug = self.company_slug
        if slug.startswith(("http://", "https://")):
            base = slug.rstrip("/")
        elif "." in slug:
            # Bare host like "job.schindler.com"
            base = f"https://{slug}"
        else:
            # Bare slug — guess `job.{slug}.com`
            base = f"https://job.{slug}.com"
        return f"{base}/sitemal.xml"

    async def _fetch_feed(
        self, client: httpx.AsyncClient, url: str
    ) -> str:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/rss+xml, application/xml, text/xml",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"SuccessFactors fetch failed for {url}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"SuccessFactors RSS feed not found: {url}"
                )
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"SuccessFactors returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"SuccessFactors returned {response.status_code} for {url}"
            )
        raise ScraperError(f"SuccessFactors exhausted retries for {url}")

    def _parse_feed(self, xml_text: str) -> list[Job]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ScraperError(
                f"SuccessFactors returned malformed XML: {exc}"
            ) from exc

        # Some tenants front the feed with an HTML error page that parses
        # as XML but isn't RSS. Catch that.
        if root.tag.lower() != "rss" and root.find(".//channel") is None:
            raise ScraperError(
                f"SuccessFactors returned non-RSS XML for {self.company_slug} "
                f"(root <{root.tag}>); tenant may not expose sitemal.xml"
            )

        company = self._derive_company_name(root)
        host = urlparse(self._resolve_feed_url()).hostname or ""

        jobs: list[Job] = []
        seen: set[str] = set()
        for item in root.iter("item"):
            job = self._parse_item(item, company=company, host=host)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _derive_company_name(self, root: ET.Element) -> str:
        title = root.findtext(".//channel/title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        return self.company_slug

    def _parse_item(
        self, item: ET.Element, *, company: str, host: str
    ) -> Job | None:
        link = (item.findtext("link") or "").strip()
        if not link:
            return None
        # ats_id: prefer the Google ID, else the trailing numeric/hash from URL.
        gid = item.findtext("g:id", namespaces=_GOOGLE_NS)
        ats_id = (gid or "").strip()
        if not ats_id:
            tail = link.rstrip("/").rsplit("/", 1)[-1]
            ats_id = re.split(r"[?&#]", tail, maxsplit=1)[0] or link
        guid = item.findtext("guid")
        if not ats_id and guid:
            ats_id = guid.strip()

        title_raw = (item.findtext("title") or "").strip() or "Untitled"
        title, location = _split_title_location(title_raw)

        # Prefer Google namespace location when present.
        if not location:
            location = _first_text(
                item.findtext("g:location", namespaces=_GOOGLE_NS),
            )

        description = _clean_description(item.findtext("description"))
        posted_at = _parse_pubdate(item.findtext("pubDate"))

        # Most SuccessFactors RSS feeds omit ``pubDate`` and instead
        # carry only ``g:expiration_date``. We don't surface that as
        # ``posted_at`` (it would be misleading) but a small subset of
        # tenants ship a non-namespaced ``date`` element with the post
        # date.
        if posted_at is None:
            for tag in ("postDate", "publishedDate", "pubdate", "date"):
                v = item.findtext(tag)
                if v:
                    posted_at = _parse_pubdate(v)
                    if posted_at:
                        break

        # ``g:employer`` is the actual employer name (the recruiting-
        # marketing channel title is typically the parent brand).
        employer = _first_text(item.findtext("g:employer", namespaces=_GOOGLE_NS))
        if employer:
            company = employer

        # ``g:job_function`` is the closest analog to a ``department``
        # facet (categories like ``Professionals`` / ``Engineering`` /
        # ``Sales``).
        department = _first_text(
            item.findtext("g:job_function", namespaces=_GOOGLE_NS),
        )

        # ``g:job_type`` (rare) — when present, map to the canonical
        # employment-type enum.
        employment_type: str | None = None
        commitment: str | None = None
        job_type_text = _first_text(
            item.findtext("g:job_type", namespaces=_GOOGLE_NS),
        ) or _first_text(item.findtext("g:employment_type", namespaces=_GOOGLE_NS))
        if job_type_text:
            commitment = job_type_text
            norm = job_type_text.lower()
            for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                if needle in norm:
                    employment_type = mapped
                    break

        return Job(
            url=link,
            title=title,
            company=company,
            ats_type=ATSType.SUCCESSFACTORS,
            ats_id=ats_id,
            location=location,
            description=description,
            department=department,
            employment_type=employment_type,
            commitment=commitment,
            posted_at=posted_at,
            fetched_at=datetime.now(),
        )


def _split_title_location(raw: str) -> tuple[str, str | None]:
    """Some tenants format titles as ``"Title (City, State, Country)"``.
    Strip the parens into a separate location. Leave the title untouched
    when the trailing parens look like a department/category instead."""
    match = _TITLE_LOCATION_RE.match(raw)
    if not match:
        return raw, None
    inner = match.group("loc").strip()
    # Heuristic: a location usually has a comma OR ends in a 2-letter
    # country/state code. Reject single-word parens like "(Remote)".
    if "," in inner or re.search(r"\b[A-Z]{2}\b", inner):
        return match.group("title").strip(), inner
    return raw, None


def _first_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10_000] or None


def _parse_pubdate(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
