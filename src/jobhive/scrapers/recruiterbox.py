"""Recruiterbox / Trakstar Hire careers scraper.

Recruiterbox (rebranded Trakstar Hire) exposes a public, unauthenticated
job-feed endpoint that does not require an API key:

    GET https://jsapi.recruiterbox.com/v1/openings?client_name={slug}&offset={n}&limit=100

Returns ``{"meta": {"offset", "limit", "total"}, "objects": [...]}`` — paginated
on the server side. We exhaust pagination internally.

Each object carries: ``id``, ``title``, ``hosted_url`` (canonical posting URL
on hire.trakstar.com), ``location`` (structured city/state/country/zipcode),
``description`` (HTML), ``allows_remote``, ``position_type``
("full_time"|...), ``team``, ``close_date``, ``client_name``.

A 400 with ``{"client_name": "Invalid client name"}`` means the slug isn't a
Recruiterbox tenant. A 200 with ``meta.total == 0`` means a valid tenant with
zero open positions.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://jsapi.recruiterbox.com/v1/openings"
PAGE_LIMIT = 100
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 25_000

_TYPE_MAP = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.RECRUITERBOX)
class RecruiterboxScraper(BaseScraper):
    """Recruiterbox / Trakstar Hire scraper.

    ``company_slug`` is the Recruiterbox ``client_name``
    (e.g. ``"demoaccount"`` → openings list at the API)."""

    ats = ATSType.RECRUITERBOX

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            offset = 0
            while True:
                payload = await self._fetch_with_retry(client, offset)
                meta = payload.get("meta") if isinstance(payload, dict) else {}
                objects = (
                    payload.get("objects") if isinstance(payload, dict) else []
                ) or []
                for item in objects:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_opening(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                # Stop conditions: empty page, or we've reached the reported total.
                if not objects:
                    break
                total = (
                    int(meta.get("total"))
                    if isinstance(meta, dict) and isinstance(meta.get("total"), int)
                    else None
                )
                offset += len(objects)
                if total is not None:
                    if offset >= total:
                        break
                elif len(objects) < PAGE_LIMIT:
                    # Only fall back to "short page = end" when the server
                    # didn't tell us the total. Some real responses return
                    # 99 instead of 100 on a non-final page; if we have
                    # `total` we trust it.
                    break
        return jobs

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, offset: int
    ) -> dict[str, Any]:
        params = {
            "client_name": self.company_slug,
            "offset": offset,
            "limit": PAGE_LIMIT,
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    API_URL,
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Recruiterbox fetch failed for {self.company_slug} "
                        f"at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Recruiterbox returned malformed JSON for "
                        f"{self.company_slug}: {exc}"
                    ) from exc
            if response.status_code == 400:
                # The API returns 400 with {"client_name": "Invalid client name"}
                # for unknown tenants — treat that as not-found.
                try:
                    err = response.json()
                except ValueError:
                    err = {}
                if isinstance(err, dict) and "Invalid client name" in str(
                    err.get("client_name", "")
                ):
                    raise CompanyNotFoundError(
                        f"Recruiterbox tenant not found: {self.company_slug}"
                    )
                raise ScraperError(
                    f"Recruiterbox 400 for {self.company_slug}: {err or response.text[:120]}"
                )
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Recruiterbox tenant not found: {self.company_slug}"
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Recruiterbox returned {response.status_code} for "
                        f"{self.company_slug} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Recruiterbox returned {response.status_code} for "
                f"{self.company_slug} at offset={offset}"
            )
        raise ScraperError(
            f"Recruiterbox exhausted retries for {self.company_slug}"
        )

    def _parse_opening(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        url = item.get("hosted_url") or item.get("url")
        if not ats_id or not title or not url:
            return None

        company = (
            (item.get("client_name") or "").strip() or self.company_slug
        )

        is_remote = item.get("allows_remote")
        if not isinstance(is_remote, bool):
            is_remote = None

        raw: dict[str, Any] = {}
        for k in ("position_type", "experience", "education", "industry",
                  "department", "category", "responsibilities"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.RECRUITERBOX,
            ats_id=ats_id,
            location=_format_location(item.get("location")),
            is_remote=is_remote,
            employment_type=_TYPE_MAP.get(
                (item.get("position_type") or "").lower()
            ),
            team=item.get("team") or None,
            commitment=item.get("position_type") if isinstance(item.get("position_type"), str) else None,
            description=_html_unescape_for_desc(item.get("description")),
            posted_at=_parse_iso(item.get("created_on") or item.get("updated_on")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        str(value[k]).strip()
        for k in ("city", "state", "country")
        if isinstance(value.get(k), str) and value.get(k).strip()
    ]
    return ", ".join(parts) or None


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure.
    Replaces the legacy _strip_html/_html_to_text path for descriptions
    only — title/company/salary fields still use the strip variant."""
    import html as _h
    if not isinstance(value, str):
        return None
    out = _h.unescape(value).strip()
    if not out:
        return None
    return out[:cap]


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:MAX_DESCRIPTION_LEN]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
