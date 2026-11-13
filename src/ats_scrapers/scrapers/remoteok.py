"""Remote OK (https://remoteok.com) — remote-only tech jobs scraper.

Remote OK is a direct-posting board: companies pay to list on Remote OK,
the listings are not syndicated from LinkedIn / Indeed. Inventory is
small (~100 active postings at any one time) but tech-focused with
structured fields (salary range, tags, location, apply URL) on every
row.

Public JSON at ``https://remoteok.com/api`` — no auth, no key, no
pagination. The single response is a list whose first entry is API
metadata (legal notice + last-updated timestamp); jobs follow.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / getonbrd / wanted pattern).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://remoteok.com/api"

_TAG_RE = re.compile(r"<[^>]+>")
# Remote OK injects an anti-bot reminder line into many descriptions —
# stripping it keeps the canonical description clean for downstream
# search / classifiers.
_ANTIBOT_RE = re.compile(
    r"Please mention the word \*\*[A-Z]+\*\* and tag [^\s]+ when applying.+?\(.+?\)\.\s*"
    r"(This is a beta feature.+?human\.)?",
    re.DOTALL,
)


@ScraperRegistry.register(ATSType.REMOTEOK)
class RemoteOKScraper(BaseScraper):
    """Remote OK (remoteok.com) — remote-only tech jobs.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper grabs the entire active board.
    """

    ats = ATSType.REMOTEOK
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_json(API_URL)
        if not isinstance(payload, list):
            raise ScraperError(
                f"Remote OK API shape changed — expected a list, "
                f"got {type(payload).__name__}"
            )
        # The response is a list whose first entry is API metadata
        # (a ``last_updated`` epoch + legal-notice text) followed by the
        # actual job entries. Every real job has an ``id``.
        seen: set[str] = set()
        jobs: list[Job] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job = self._parse_job(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "")
        title = (item.get("position") or item.get("title") or "").strip()
        company = (item.get("company") or "").strip()
        url = item.get("url")
        if not (ats_id and title and url):
            return None

        location = _normalize_location(item.get("location"))
        salary_min = _to_float(item.get("salary_min"))
        salary_max = _to_float(item.get("salary_max"))
        salary_currency = "USD" if (salary_min or salary_max) else None

        description = _clean_description(item.get("description"))
        posted_at = _epoch_to_dt(item.get("epoch")) or _iso_to_dt(item.get("date"))

        tags = item.get("tags") or []
        if isinstance(tags, list):
            tags_clean: list[str] = [t for t in tags if isinstance(t, str)]
        else:
            tags_clean = []

        raw: dict[str, Any] = {}
        if tags_clean:
            raw["tags"] = tags_clean[:30]
        if item.get("verified"):
            raw["verified"] = item["verified"]
        if item.get("original"):
            raw["original_post_id"] = item["original"]

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.REMOTEOK,
            ats_id=ats_id,
            location=location,
            is_remote=True,  # Remote OK is, by definition, remote-only.
            salary_currency=salary_currency,
            salary_period="YEAR",
            salary_min=salary_min,
            salary_max=salary_max,
            apply_url=item.get("apply_url"),
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _normalize_location(value: object) -> str | None:
    """Remote OK's ``location`` is freeform — often empty or 'Worldwide';
    sometimes a country/region restriction (e.g. 'United States').
    Pass through verbatim, returning None for blanks."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _to_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _epoch_to_dt(value: object) -> datetime | None:
    """Remote OK's ``epoch`` is unix-seconds, not ms."""
    try:
        sec = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if sec <= 0:
        return None
    return datetime.fromtimestamp(sec, tz=UTC)


def _iso_to_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_description(value: object) -> str | None:
    """Strip Remote OK's anti-bot reminder line + HTML, collapse whitespace,
    and truncate to the canonical ~25k chars budget."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _ANTIBOT_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:25_000] or None
