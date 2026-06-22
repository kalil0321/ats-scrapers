"""Ashby scraper.

Ashby exposes a public JSON board at:
    https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

The compensation field, when present, is rich (range + currency + interval).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_INTERVAL_MAP = {
    "HOURLY": "HOUR",
    "DAILY": "DAY",
    "WEEKLY": "WEEK",
    "MONTHLY": "MONTH",
    "ANNUALLY": "YEAR",
    "YEARLY": "YEAR",
    # Ashby's newer format uses "1 YEAR", "1 HOUR", etc.
    "1 YEAR": "YEAR",
    "1 MONTH": "MONTH",
    "1 WEEK": "WEEK",
    "1 DAY": "DAY",
    "1 HOUR": "HOUR",
}

_EMPLOYMENT_TYPE_MAP = {
    "FULLTIME": "FULL_TIME",
    "FULL_TIME": "FULL_TIME",
    "PARTTIME": "PART_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "INTERNSHIP": "INTERN",
    "INTERN": "INTERN",
    "TEMPORARY": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.ASHBY)
class AshbyScraper(BaseScraper):
    ats = ATSType.ASHBY

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
                        f"Ashby fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"Ashby board not found: {self.company_slug}"
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Ashby ({self.company_slug}) returned "
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
                f"Ashby returned {response.status_code} for {self.company_slug}"
            )
        raise ScraperError(f"Ashby ({self.company_slug}) exhausted retries")

    def _parse_job(self, item: dict[str, Any]) -> Job:
        comp = item.get("compensation") or {}
        summary = (
            comp.get("compensationTierSummary")
            or comp.get("scrapeableCompensationSalarySummary")
        )
        salary_min, salary_max, currency, period = _parse_comp(comp)

        emp_type = (item.get("employmentType") or "").upper()
        employment_type = _EMPLOYMENT_TYPE_MAP.get(emp_type)

        # ``isRemote`` is only set when truly remote; ``workplaceType``
        # ("Remote" / "On-site" / "Hybrid") fills the gap. Hybrid stays
        # None — neither flag captures it cleanly.
        is_remote = item.get("isRemote") if isinstance(item.get("isRemote"), bool) else None
        if is_remote is None:
            wp = item.get("workplaceType")
            if isinstance(wp, str):
                wp_norm = wp.strip().lower().replace("-", "").replace(" ", "")
                if wp_norm == "remote":
                    is_remote = True
                elif wp_norm in ("onsite", "inperson", "office"):
                    is_remote = False

        # Description — prefer ``descriptionHtml`` over ``descriptionPlain``.
        # The HTML form retains paragraph breaks, bullet lists, and headings
        # (the plain text concatenates them into a single block); the
        # post-scrape markdownify step in scripts/normalize_descriptions.py
        # then converts the HTML into clean markdown. Plain stays as a
        # last-ditch fallback.
        description = (
            item.get("descriptionHtml")
            or item.get("descriptionPlain")
            or None
        )

        secondary_locations = item.get("secondaryLocations") or []

        raw: dict[str, Any] = {}
        if item.get("department"):
            raw["department"] = item["department"]
        if item.get("team"):
            raw["team"] = item["team"]
        if secondary_locations:
            raw["secondary_locations"] = [
                loc.get("location") for loc in secondary_locations
                if isinstance(loc, dict) and loc.get("location")
            ]
        if item.get("address"):
            raw["address"] = item["address"]
        if item.get("workplaceType"):
            raw["workplace_type"] = item["workplaceType"]
        if comp:
            # Keep the full compensation tier structure for downstream consumers
            # who want to surface bonus/equity/commission separately.
            raw["compensation_tiers"] = comp.get("compensationTiers")

        return Job(
            url=item.get("jobUrl") or item.get("applyUrl"),
            title=item["title"],
            company=self.company_slug,
            ats_type=ATSType.ASHBY,
            ats_id=item["id"],
            location=item.get("location"),
            is_remote=is_remote,
            description=description,
            employment_type=employment_type,
            department=item.get("department") if isinstance(item.get("department"), str) else None,
            team=item.get("team") if isinstance(item.get("team"), str) else None,
            apply_url=item.get("applyUrl") if item.get("applyUrl") != item.get("jobUrl") else None,
            salary_currency=currency,
            salary_period=period,
            salary_summary=summary,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_at=_parse_iso(item.get("publishedAt")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _parse_comp(
    comp: dict[str, Any] | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Pull structured min/max/currency/period from a compensation tier.

    Ashby returns multiple component types (Salary, Bonus, Commission, Equity*).
    Salary is the field we want to surface as min/max. Field names live at the
    component level (`minValue`, `maxValue`, `currencyCode`) — not nested in
    `compensationValue` as some older docs suggest.
    """
    if not comp:
        return None, None, None, None
    for tier in comp.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if component.get("compensationType") != "Salary":
                continue
            interval = _INTERVAL_MAP.get(component.get("interval", ""), "YEAR")
            return (
                component.get("minValue"),
                component.get("maxValue"),
                component.get("currencyCode"),
                interval,
            )
    return None, None, None, None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
