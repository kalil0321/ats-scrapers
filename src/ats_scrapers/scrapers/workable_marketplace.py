"""Workable job marketplace (https://jobs.workable.com).

Workable publishes every live posting from its customers on a public
job search at ``jobs.workable.com``. The page is backed by a JSON API:

    GET https://jobs.workable.com/api/v1/jobs?query=&location=&pageToken=

Each page holds 20 postings, each with the full description, the
requirements and benefits sections, a structured location and the
employer (id, name, website). Paging uses an opaque ``nextPageToken``
(a search-after cursor), so the walk has no depth limit: one pass
returns the whole marketplace (about 170k postings in Nov 2026).

These are employer postings hosted on Workable itself, so they belong
with the ATS sources. The per-tenant widget scraper (``workable``)
needs one request per employer and a list of employer slugs; this
scraper needs neither, which is why it covers far more employers.

The marketplace does not expose the widget ``shortcode``, so its
``ats_id`` is the marketplace job id (a UUID) and its ``ats_type`` is
``workable_marketplace``. Rows from the two Workable scrapers therefore
never share a ``global_id``.

The API rate-limits bursts from one IP. The walk is sequential with a
pause between pages (``page_delay``); 429 and 5xx responses are retried
by the shared fetcher, honouring ``Retry-After``.

Single-source scraper: ``company_slug`` is ignored.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://jobs.workable.com/api/v1/jobs"
MAX_DESCRIPTION_LEN = 25_000

# Seconds between page requests. About 1 request per second stayed
# below the rate limit in a 45-page test on 2026-11-12.
DEFAULT_PAGE_DELAY = 0.7
# Safety stop: 20 postings per page, so 20k pages is 400k postings,
# well above the ~170k live today.
DEFAULT_MAX_PAGES = 20_000

_BLOCK_TAG_RE = re.compile(
    r"<\s*(?:br|/p|/div|/h[1-6]|/tr|/ul|/ol)\s*/?\s*>", re.IGNORECASE,
)
_LI_OPEN_RE = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n\s*\n+")

_EMPLOYMENT_TYPES = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "temporary": "TEMPORARY",
    "internship": "INTERN",
}

# Country names as the marketplace spells them (``location.countryName``).
# Names not listed here leave ``country_iso`` unset rather than guessed.
_COUNTRY_ISO = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "bahrain": "BH",
    "belgium": "BE", "brazil": "BR", "bulgaria": "BG", "canada": "CA",
    "chile": "CL", "china": "CN", "colombia": "CO", "costa rica": "CR",
    "croatia": "HR", "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ",
    "denmark": "DK", "egypt": "EG", "estonia": "EE", "finland": "FI",
    "france": "FR", "germany": "DE", "ghana": "GH", "greece": "GR",
    "hong kong": "HK", "hungary": "HU", "india": "IN", "indonesia": "ID",
    "ireland": "IE", "israel": "IL", "italy": "IT", "japan": "JP",
    "jordan": "JO", "kenya": "KE", "kuwait": "KW", "latvia": "LV",
    "lebanon": "LB", "lithuania": "LT", "luxembourg": "LU", "malaysia": "MY",
    "malta": "MT", "mexico": "MX", "morocco": "MA", "netherlands": "NL",
    "new zealand": "NZ", "nigeria": "NG", "norway": "NO", "oman": "OM",
    "pakistan": "PK", "peru": "PE", "philippines": "PH", "poland": "PL",
    "portugal": "PT", "qatar": "QA", "romania": "RO", "saudi arabia": "SA",
    "serbia": "RS", "singapore": "SG", "slovakia": "SK", "slovenia": "SI",
    "south africa": "ZA", "south korea": "KR", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "taiwan": "TW", "thailand": "TH", "turkey": "TR",
    "türkiye": "TR", "ukraine": "UA", "united arab emirates": "AE",
    "united kingdom": "GB", "united states": "US", "uruguay": "UY",
    "vietnam": "VN",
}


@ScraperRegistry.register(ATSType.WORKABLE_MARKETPLACE)
class WorkableMarketplaceScraper(BaseScraper):
    """Every live posting on the Workable marketplace, in one walk.

    Single-source scraper: ``company_slug`` is ignored.
    """

    ats = ATSType.WORKABLE_MARKETPLACE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "application/json",
    }

    def __init__(
        self,
        company_slug: str = "any",
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        page_delay: float = DEFAULT_PAGE_DELAY,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.page_delay = page_delay
        self.max_pages = max_pages

    async def afetch(self) -> list[Job]:
        jobs: dict[str, Job] = {}
        total: int | None = None
        token: str | None = None
        pages = 0
        async with self.make_fetcher() as fetch:
            while pages < self.max_pages:
                params = {"query": "", "location": ""}
                if token:
                    params["pageToken"] = token
                payload = await fetch.get_json(API_URL, params=params)
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("jobs"), list
                ):
                    raise ScraperError(
                        "Workable marketplace API shape changed: "
                        "expected an object with a 'jobs' list"
                    )
                pages += 1
                if total is None and isinstance(payload.get("totalSize"), int):
                    total = payload["totalSize"]
                for item in payload["jobs"]:
                    if not isinstance(item, dict):
                        continue
                    job = _parse_job(item)
                    if job is not None and job.ats_id:
                        jobs.setdefault(job.ats_id, job)
                next_token = payload.get("nextPageToken")
                if not payload["jobs"] or not isinstance(next_token, str):
                    break
                if next_token == token:
                    raise ScraperError(
                        "Workable marketplace returned the same page token twice"
                    )
                token = next_token
                if self.page_delay > 0:
                    await asyncio.sleep(self.page_delay)
            else:
                raise ScraperError(
                    f"Workable marketplace walk hit max_pages={self.max_pages} "
                    "before the last page"
                )
        if total and len(jobs) < total * 0.5:
            # The cursor walk stopped far short of the reported total:
            # publishing would silently drop most of the marketplace.
            raise ScraperError(
                f"Workable marketplace returned {len(jobs)} postings, "
                f"less than half of the reported {total}"
            )
        return list(jobs.values())


def _parse_job(item: dict[str, Any]) -> Job | None:
    job_id = item.get("id")
    title = item.get("title")
    url = item.get("url")
    if not (isinstance(job_id, str) and job_id and isinstance(title, str)
            and title.strip() and isinstance(url, str) and url):
        return None
    if item.get("state") not in (None, "published"):
        return None

    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    company_name = company.get("title") if isinstance(company.get("title"), str) else None

    location, country_iso = _location(item)
    workplace = item.get("workplace")
    is_remote: bool | None = None
    if workplace == "remote":
        is_remote = True
    elif workplace in ("on_site", "onsite", "hybrid"):
        is_remote = False

    employment_raw = item.get("employmentType")
    employment_type = None
    commitment = None
    if isinstance(employment_raw, str) and employment_raw.strip():
        commitment = employment_raw.strip()
        employment_type = _EMPLOYMENT_TYPES.get(commitment.lower())

    department = item.get("department")
    language = item.get("language")

    raw: dict[str, Any] = {}
    if company.get("id"):
        raw["company_id"] = company["id"]
    if isinstance(company.get("website"), str) and company["website"]:
        raw["company_website"] = company["website"]
    if isinstance(company.get("url"), str) and company["url"]:
        raw["company_page"] = company["url"]
    if workplace:
        raw["workplace"] = workplace
    locations = item.get("locations")
    if isinstance(locations, list) and len(locations) > 1:
        raw["locations"] = [loc for loc in locations if isinstance(loc, str)][:20]

    return Job(
        url=url,
        title=title.strip(),
        company=(company_name or "").strip() or "Unknown",
        ats_type=ATSType.WORKABLE_MARKETPLACE,
        ats_id=job_id,
        location=location,
        country_iso=country_iso,
        is_remote=is_remote,
        department=department.strip() if isinstance(department, str) and department.strip() else None,
        employment_type=employment_type,
        commitment=commitment,
        language=language if isinstance(language, str) and language else None,
        description=_description(item),
        posted_at=_parse_iso(item.get("created")),
        fetched_at=datetime.now(UTC),
        raw=raw or None,
    )


def _location(item: dict[str, Any]) -> tuple[str | None, str | None]:
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    country = loc.get("countryName") if isinstance(loc.get("countryName"), str) else None
    parts = [
        loc.get("city"), loc.get("subregion"), country,
    ]
    text = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    country_iso = _COUNTRY_ISO.get(country.strip().lower()) if country else None
    if not text:
        locations = item.get("locations")
        if isinstance(locations, list):
            named = [x for x in locations if isinstance(x, str) and x != "TELECOMMUTE"]
            text = " | ".join(named)
    return (text or None), country_iso


def _description(item: dict[str, Any]) -> str | None:
    sections = [
        (None, item.get("description")),
        ("Requirements", item.get("requirementsSection")),
        ("Benefits", item.get("benefitsSection")),
    ]
    blocks: list[str] = []
    for heading, value in sections:
        text = _html_to_text(value)
        if text:
            blocks.append(f"{heading}\n{text}" if heading else text)
    joined = "\n\n".join(blocks).strip()
    return joined[:MAX_DESCRIPTION_LEN] or None


def _html_to_text(value: object) -> str | None:
    """HTML to plain text that keeps block breaks but not inline ones.

    Line breaks go only where a block ends (paragraph, list item, heading,
    ``<br>``), so ``We offer <b>great</b> pay.`` stays on one line.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = _LI_OPEN_RE.sub("\n- ", value)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [_INLINE_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    return text or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
