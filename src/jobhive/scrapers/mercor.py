"""Mercor scraper.

Mercor is a talent marketplace where companies post contract roles. The
public ``work.mercor.com`` site is a Next.js CSR app — its old SSR
``__NEXT_DATA__`` blob is now empty post-CSR-migration. The actual data
source is the JSON API the page hydrates from:

    GET https://aws.api.mercor.com/work/listings-explore-page

Returns ``{"listings": [...]}`` — every listing in one response, no
pagination. Each listing carries title, company, location, rate, and the
full description, so we don't need an N+1 detail fetch.

The endpoint accepts a literal ``Authorization: Bearer`` header with **no
token** — that's how Mercor scopes anonymous explore access. Origin/Referer
headers are required (the API checks them).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://aws.api.mercor.com/work/listings-explore-page"
WORK_BASE_URL = "https://work.mercor.com"

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # Literal `Bearer` with no token — Mercor uses this for anonymous
    # explore access. Removing it returns 401.
    "Authorization": "Bearer",
    "Origin": "https://work.mercor.com",
    "Referer": "https://work.mercor.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
    "X-Client-Ip": "true",
}

# Mercor's pay frequencies map to our SalaryPeriod enum.
_FREQUENCY_MAP = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "yearly": "YEAR",
    "annually": "YEAR",
}


@ScraperRegistry.register(ATSType.MERCOR)
class MercorScraper(BaseScraper):
    """Mercor scraper. ``company_slug`` is informational — Mercor's explore
    endpoint is global (one feed of contract listings across all companies)."""

    ats = ATSType.MERCOR

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            payload = await self._fetch_with_retry(client)
        listings = payload.get("listings") or []
        seen: set[str] = set()
        jobs: list[Job] = []
        for item in listings:
            if not isinstance(item, dict):
                continue
            job = _parse_listing(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient
    ) -> dict[str, Any]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(API_URL, headers=_HEADERS)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(f"Mercor fetch failed: {exc}") from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(f"Mercor returned malformed JSON: {exc}") from exc
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Mercor returned {response.status_code} after "
                        f"{MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(f"Mercor returned {response.status_code}")
        raise ScraperError("Mercor exhausted retries")


def _parse_listing(item: dict[str, Any]) -> Job | None:
    listing_id = str(item.get("listingId") or "").strip()
    title = (item.get("title") or "").strip()
    if not listing_id or not title:
        return None

    company = (item.get("companyName") or "").strip() or "Mercor"
    url = f"{WORK_BASE_URL}/jobs/{listing_id}/{_slugify(title)}"

    rate_min = _to_float(item.get("rateMin"))
    rate_max = _to_float(item.get("rateMax"))
    pay_freq = (item.get("payRateFrequency") or "").lower()
    salary_period = _FREQUENCY_MAP.get(pay_freq) if pay_freq else None

    # Mercor is a contract talent marketplace — every listing is a
    # contract role regardless of the rate frequency. ``commitment``
    # in the Mercor API is the rate frequency (``hourly`` / ``weekly``
    # / etc.), not an employment-type label, so default to CONTRACT
    # and surface the rate frequency in ``commitment`` for display.
    employment_type = "CONTRACT"
    commitment_raw = item.get("commitment")
    commitment: str | None = None
    if isinstance(commitment_raw, str) and commitment_raw.strip():
        # Normalise "hourly" → "Hourly" for display; downstream UI
        # likes title-case labels.
        commitment = commitment_raw.strip().capitalize()
    hours = item.get("hoursPerWeek")
    if isinstance(hours, (int, float)) and hours > 0:
        # Append hours/week to the commitment label when present.
        commitment = (
            f"{commitment} · {int(hours)}h/week"
            if commitment else f"{int(hours)}h/week"
        )

    # ``workArrangement`` is the canonical remote/hybrid/onsite signal.
    # ``location`` text often duplicates it ("Remote") so we set
    # ``is_remote`` from the structured field for reliability.
    work_arrangement = (item.get("workArrangement") or "").strip().lower()
    is_remote: bool | None = None
    if work_arrangement == "remote":
        is_remote = True
    elif work_arrangement in ("onsite", "on-site", "in-office", "office"):
        is_remote = False

    salary_summary = _build_salary_summary(rate_min, rate_max, pay_freq)

    raw_desc = item.get("description")
    description = raw_desc.strip()[:25_000] or None if isinstance(raw_desc, str) else None

    raw: dict[str, Any] = {}
    for k in ("commitment", "category", "skills", "tags",
              "experienceLevel", "remote", "tier", "workArrangement",
              "eligibleResidenceLocation", "ineligibleResidenceLocation",
              "offersEquity", "hoursPerWeek"):
        v = item.get(k)
        if v not in (None, "", [], False):
            raw[k] = v

    return Job(
        url=url,
        title=title,
        company=company,
        ats_type=ATSType.MERCOR,
        ats_id=listing_id,
        location=(item.get("location") or "").strip() or None,
        is_remote=is_remote,
        salary_min=rate_min,
        salary_max=rate_max,
        salary_currency="USD" if (rate_min or rate_max) else None,
        salary_period=salary_period,
        salary_summary=salary_summary,
        employment_type=employment_type,
        commitment=commitment,
        description=description,
        posted_at=_parse_iso(item.get("postedAt")),
        fetched_at=datetime.now(),
        raw=raw or None,
    )


def _build_salary_summary(
    rate_min: float | None,
    rate_max: float | None,
    pay_freq: str,
) -> str | None:
    """Format a human-readable USD pay range.

    Mercor always pays USD; the public listing only ships rate min/max
    plus a frequency label. We surface ``$55–65/hour`` style strings so
    consumers don't have to format from the structured triple.
    """
    if rate_min is None and rate_max is None:
        return None
    suffix = f"/{pay_freq}" if pay_freq else ""
    if rate_min == rate_max and rate_min is not None:
        return f"${rate_min:,.0f}{suffix}"
    if rate_min is None:
        return f"up to ${rate_max:,.0f}{suffix}"
    if rate_max is None:
        return f"from ${rate_min:,.0f}{suffix}"
    return f"${rate_min:,.0f}–{rate_max:,.0f}{suffix}"


def _slugify(text: str) -> str:
    """URL-friendly slug: strip punctuation, collapse spaces to ``-``."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", s).strip("-")


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
