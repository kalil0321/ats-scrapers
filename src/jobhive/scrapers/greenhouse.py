"""Greenhouse scraper.

Greenhouse exposes a public JSON board at:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

The most permissive ATS API — no auth, no rate limits in practice. The
``content=true`` flag inflates the response with full job descriptions
(HTML-encoded entities — we decode + strip tags). First_published gives
the canonical posted-at; updated_at is the better choice for "when this
posting changed" but we surface first_published since it's stable.

The list response also carries ``departments`` (array of named groups)
and ``offices`` (locations the role is open in). Employment type is
NOT in the list response — Greenhouse doesn't expose it on the public
board API, only via the authenticated harvest API.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

# ``content=true`` opts the API into returning the full HTML description
# in each job entry. The flag adds ~5x to the response size but saves
# us per-job detail fetches across ~3,000 boards.
API_TEMPLATE = (
    "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.GREENHOUSE)
class GreenhouseScraper(BaseScraper):
    ats = ATSType.GREENHOUSE

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            payload = await self._fetch_with_retry(client, url)
        return [self._parse_job(item) for item in payload.get("jobs", [])]

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> dict[str, Any]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Greenhouse fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Greenhouse board not found: {self.company_slug}"
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Greenhouse ({self.company_slug}) returned "
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
                f"Greenhouse returned {response.status_code} for {self.company_slug}"
            )
        raise ScraperError(f"Greenhouse ({self.company_slug}) exhausted retries")

    def _parse_job(self, item: dict[str, Any]) -> Job:
        offices = item.get("offices") or []
        departments = item.get("departments") or []
        first_dept = next(
            (d.get("name") for d in departments if isinstance(d, dict) and d.get("name")),
            None,
        )
        metadata = item.get("metadata") or []
        # Greenhouse "metadata" is a list of {name, value, value_type} dicts —
        # custom fields the employer set. Capture verbatim in ``raw``.
        raw: dict[str, Any] = {}
        if metadata:
            raw["metadata"] = metadata
        if departments:
            raw["departments"] = [d.get("name") for d in departments if isinstance(d, dict)]
        if offices:
            raw["offices"] = [o.get("name") for o in offices if isinstance(o, dict)]
        if item.get("internal_job_id") is not None:
            raw["internal_job_id"] = item["internal_job_id"]

        # Greenhouse's ``content`` is HTML-encoded twice on the public
        # API (the entities are escaped, then the whole string wrapped):
        # ``&lt;h2&gt;`` etc. We unescape once, then strip tags to plain
        # text for storage.
        description = _clean_description(item.get("content"))

        # ``first_published`` is a stable creation timestamp (employer
        # set when the posting first went live). ``updated_at`` only
        # tells us when an internal field changed (often noise). Prefer
        # first_published for "posted_at" semantics.
        posted_at = _parse_iso(item.get("first_published")) or _parse_iso(
            item.get("updated_at")
        )

        # ``requisition_id`` is sometimes a placeholder ("See Opening
        # ID", "TBD"); only keep when it looks like a real identifier.
        req_raw = item.get("requisition_id")
        requisition_id: str | None = None
        if isinstance(req_raw, (str, int)):
            req_str = str(req_raw).strip()
            if req_str and req_str.lower() not in (
                "see opening id", "tbd", "n/a", "tba",
            ):
                requisition_id = req_str

        return Job(
            url=item["absolute_url"],
            title=item["title"],
            company=self.company_slug,
            ats_type=ATSType.GREENHOUSE,
            ats_id=str(item["id"]),
            location=(item.get("location") or {}).get("name"),
            department=first_dept,
            description=description,
            requisition_id=requisition_id,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # Greenhouse double-encodes ``content``: ``<div>`` shows up as
    # ``&lt;div&gt;``. Unescape once to recover real HTML; then leave the
    # tags in place so the post-scrape markdownify step can preserve
    # paragraph breaks, bullet lists, and headings. The previous brutal
    # tag-strip collapsed the body into a single space-separated blob,
    # losing all visual structure.
    text = html_mod.unescape(value).strip()
    return text[:25_000] or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
