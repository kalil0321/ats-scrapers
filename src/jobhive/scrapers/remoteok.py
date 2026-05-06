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

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://remoteok.com/api"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

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

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            payload = await self._fetch_with_retry(client)
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

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    API_URL, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Remote OK fetch failed: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Remote OK returned non-JSON: {exc}"
                    ) from exc
                if not isinstance(data, list):
                    raise ScraperError(
                        f"Remote OK API shape changed — expected a list, "
                        f"got {type(data).__name__}"
                    )
                return data
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Remote OK returned {response.status_code} after "
                        f"{MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Remote OK returned {response.status_code}"
            )
        raise ScraperError(f"Remote OK exhausted retries: {last_exc}")

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
            fetched_at=datetime.now(),
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
    return datetime.fromtimestamp(sec)


def _iso_to_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_description(value: object) -> str | None:
    """Strip Remote OK's anti-bot reminder line + HTML, collapse whitespace,
    and truncate to the canonical ~10kB budget."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _ANTIBOT_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10_000] or None
