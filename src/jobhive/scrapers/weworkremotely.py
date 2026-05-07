"""We Work Remotely (https://weworkremotely.com) — remote-only direct postings.

Companies pay to list on WWR — listings are not syndicated from
LinkedIn / Indeed. Tech-heavy with rich structured fields per row
(country, region, state, skills, expires_at, employment type).

The all-jobs RSS feed (``/remote-jobs.rss``) caps at 100 items and is
NOT paginated (``?page=2`` returns the same 100). To get the full
~500-job board we scrape the **10 category feeds** in parallel and
dedupe on ``<guid>``. Empirically only 100 of 533 unique items appear
in the main feed; the other 433 are category-only — so the main feed
alone would lose ~80% of WWR coverage.

Single-source scraper: ``company_slug`` is informational and ignored.
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

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://weworkremotely.com"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# WWR splits postings into 10 stable category feeds. We scrape all of
# them in parallel and dedupe on the per-item ``<guid>``. The set is
# baked in (rather than discovered from the home page) so the scraper
# stays deterministic when WWR rotates featured-category tiles in the
# header. New categories rarely appear; if WWR adds one this list
# would silently miss it (rare, recoverable in a follow-up).
_CATEGORY_FEEDS = (
    "all-other-remote-jobs",
    "remote-back-end-programming-jobs",
    "remote-customer-support-jobs",
    "remote-design-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-front-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-management-and-finance-jobs",
    "remote-product-jobs",
    "remote-sales-and-marketing-jobs",
)

_TAG_RE = re.compile(r"<[^>]+>")
# Job titles on WWR are formatted "Company: Job Title" (e.g.
# "Praia Health: Senior Backend Engineer"). Split on the first colon
# when both halves look meaningful so company and title are cleanly
# separated. If the title doesn't fit the pattern we leave it alone.
_TITLE_COLON_RE = re.compile(r"^(?P<co>[^:]{1,80}):\s+(?P<rest>.{2,})$")


@ScraperRegistry.register(ATSType.WEWORKREMOTELY)
class WeWorkRemotelyScraper(BaseScraper):
    """We Work Remotely (weworkremotely.com) — remote-only direct postings.

    Single-source: ``company_slug`` is ignored. Pass anything (``"any"``,
    ``""``) — the scraper enumerates all 10 category feeds.
    """

    ats = ATSType.WEWORKREMOTELY

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[ET.Element]) -> None:
            async with lock:
                for it in items:
                    job = self._parse_item(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            async def per_category(slug: str) -> None:
                xml_text = await self._fetch_feed(client, slug)
                items = _parse_feed(xml_text)
                await absorb(items)

            await asyncio.gather(*(per_category(c) for c in _CATEGORY_FEEDS))
        return jobs

    async def _fetch_feed(
        self, client: httpx.AsyncClient, category_slug: str
    ) -> str:
        url = f"{API_ROOT}/categories/{category_slug}.rss"
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/rss+xml, application/xml, text/xml",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"WWR fetch failed for {url}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"WWR returned {response.status_code} for {url} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"WWR returned {response.status_code} for {url}"
            )
        raise ScraperError(f"WWR exhausted retries for {url}: {last_exc}")

    def _parse_item(self, item: ET.Element) -> Job | None:
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not guid or not link:
            return None

        title_raw = (item.findtext("title") or "").strip() or "Untitled"
        company, title = _split_company_title(title_raw)

        location = _format_location(
            country=(item.findtext("country") or "").strip(),
            region=(item.findtext("region") or "").strip(),
            state=(item.findtext("state") or "").strip(),
        )

        description = _clean_description(item.findtext("description"))
        posted_at = _parse_pubdate(item.findtext("pubDate"))
        commitment = (item.findtext("type") or "").strip() or None

        raw: dict[str, Any] = {}
        skills = (item.findtext("skills") or "").strip()
        if skills:
            # Stored as a comma-separated string; pre-split so downstream
            # consumers don't have to re-parse it.
            raw["skills"] = [s.strip() for s in skills.split(",") if s.strip()]
        category = (item.findtext("category") or "").strip()
        if category:
            raw["category"] = category
        expires_at = (item.findtext("expires_at") or "").strip()
        if expires_at:
            raw["expires_at"] = expires_at

        return Job(
            url=link,
            title=title or "Untitled",
            company=company or "Unknown",
            ats_type=ATSType.WEWORKREMOTELY,
            ats_id=guid,
            location=location,
            is_remote=True,  # WWR is, by definition, remote-only.
            commitment=commitment,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _parse_feed(xml_text: str) -> list[ET.Element]:
    """Parse a WWR RSS feed and return the ``<item>`` elements."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ScraperError(f"WWR returned malformed RSS: {exc}") from exc
    items = root.findall(".//item")
    return list(items)


def _split_company_title(raw: str) -> tuple[str, str]:
    """Titles are formatted ``"Company: Job Title"``. Split on the first
    colon when both halves look like real strings; leave alone otherwise.
    """
    match = _TITLE_COLON_RE.match(raw)
    if not match:
        return "", raw
    co = match.group("co").strip()
    rest = match.group("rest").strip()
    if not co or not rest:
        return "", raw
    return co, rest


def _format_location(*, country: str, region: str, state: str) -> str | None:
    """Combine the populated location fields into a single readable string.

    WWR's ``region`` (and occasionally ``country``) is often
    'Anywhere in the World' — that's a semantic 'remote-eligible
    globally' tag, not a real geo. When a more specific field has a
    real value we drop the 'Anywhere…' marker; when it's the only
    signal we keep it so remote-only rows aren't blank.
    """
    def is_anywhere(v: str) -> bool:
        return v.lower().startswith("anywhere")

    specific = [v for v in (state, country) if v and not is_anywhere(v)]
    if region and not is_anywhere(region):
        specific.append(region)

    if specific:
        # Drop duplicates while preserving order (state may equal region
        # for broad postings — collapse those).
        seen: set[str] = set()
        unique = [p for p in specific if not (p in seen or seen.add(p))]
        return ", ".join(unique)

    # Nothing specific — fall back to whichever Anywhere-ish field
    # populated (e.g. region='Anywhere in the World').
    for v in (region, country, state):
        if v:
            return v
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
