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

import html as html_mod
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

# ``content=true`` opts the API into returning the full HTML description
# in each job entry. The flag adds ~5x to the response size but saves
# us per-job detail fetches across ~3,000 boards.
API_TEMPLATE = (
    "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
)

_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.GREENHOUSE)
class GreenhouseScraper(BaseScraper):
    ats = ATSType.GREENHOUSE

    async def afetch(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_json(url)
        return [self._parse_job(item) for item in payload.get("jobs", [])]

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
            fetched_at=datetime.now(UTC),
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
