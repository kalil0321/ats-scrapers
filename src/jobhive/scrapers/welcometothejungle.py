"""Welcome to the Jungle scraper (Algolia-backed).

WTTJ exposes its job listings via a public Algolia index. We hit the search
endpoint directly with the in-page public credentials. Companies post their
own jobs (it's a hybrid jobboard / ATS), so this is a first-party source —
not an aggregator.

Typical sizes:
    wttj_jobs_production_en   ~80,000+ active postings
    (other language indices are translated mirrors of the same set)

Usage:
    WTTJScraper("*").fetch()                # all jobs (default)
    WTTJScraper("auchan").fetch()           # only Auchan postings
    WTTJScraper("*", language="fr").fetch() # French translation index
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Public Algolia credentials, embedded in WTTJ's HTML so any user agent can read.
APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"

PAGE_SIZE = 1000  # Algolia max per request

# Algolia caps single-query traversal at ~1000 hits — for full traversal we
# bucket queries by published_at_timestamp window and paginate within each
# bucket. 80k jobs → 80 buckets of 1000 max.
HEADERS = {
    "x-algolia-application-id": APP_ID,
    "x-algolia-api-key": API_KEY,
    "content-type": "application/json",
    "origin": "https://www.welcometothejungle.com",
    "referer": "https://www.welcometothejungle.com/",
    "user-agent": "Mozilla/5.0",
}

CONTRACT_TYPE_MAP = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "internship": "INTERN",
    "apprenticeship": "INTERN",
    "vie": "TEMPORARY",
}

SALARY_PERIOD_MAP = {
    "yearly": "YEAR",
    "monthly": "MONTH",
    "weekly": "WEEK",
    "daily": "DAY",
    "hourly": "HOUR",
}


@ScraperRegistry.register(ATSType.WELCOMETOTHEJUNGLE)
class WTTJScraper(BaseScraper):
    """Welcome to the Jungle scraper.

    `company_slug` semantics:
      * `"*"` or `"all"` (default) — fetch every job on the platform
      * any other value — used as Algolia organization-reference filter
    """

    ats = ATSType.WELCOMETOTHEJUNGLE

    def __init__(
        self,
        company_slug: str = "*",
        *,
        language: str = "en",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.language = language
        # Default index for company-filtered queries. The full-walk path uses
        # the `_published_at_desc` replica so the cursor strategy is reliable.
        self.index = f"wttj_jobs_production_{language}"
        self._url = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{self.index}/query"
        sorted_index = f"{self.index}_published_at_desc"
        self._sorted_url = (
            f"https://{APP_ID}-dsn.algolia.net/1/indexes/{sorted_index}/query"
        )

    def fetch(self) -> list[Job]:
        """Fetch every matching job. Uses a `published_at_timestamp` cursor
        to walk past Algolia's hard 1000-result-per-query cap."""
        all_jobs: list[Job] = []
        seen: set[str] = set()
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            if self.company_slug not in ("*", "all", ""):
                # Per-org filter: well under 1000, simple page loop suffices
                for page in range(9):
                    hits = self._query(client, page=page)
                    if not hits:
                        break
                    for hit in hits:
                        obj_id = hit.get("objectID")
                        if obj_id in seen:
                            continue
                        seen.add(obj_id)
                        all_jobs.append(self._parse_hit(hit))
                    if len(hits) < PAGE_SIZE:
                        break
            else:
                # Full-platform walk via timestamp cursor
                cursor_ts: int | None = None
                page_count = 0
                max_pages = 200  # safety bound — handles up to 200k jobs
                while page_count < max_pages:
                    hits = self._query(client, cursor_ts=cursor_ts)
                    if not hits:
                        break
                    new_count = 0
                    oldest_ts = cursor_ts
                    for hit in hits:
                        obj_id = hit.get("objectID")
                        if obj_id in seen:
                            continue
                        seen.add(obj_id)
                        all_jobs.append(self._parse_hit(hit))
                        new_count += 1
                        ts = hit.get("published_at_timestamp")
                        if isinstance(ts, (int, float)) and (
                            oldest_ts is None or ts < oldest_ts
                        ):
                            oldest_ts = int(ts)
                    if new_count == 0 or oldest_ts == cursor_ts:
                        break
                    cursor_ts = oldest_ts
                    page_count += 1
                    logger.debug(
                        "WTTJ cursor walk: %d unique jobs after %d pages, cursor=%s",
                        len(all_jobs), page_count, cursor_ts,
                    )
        return all_jobs

    def _query(
        self,
        client: httpx.Client,
        *,
        page: int = 0,
        cursor_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        params = [f"hitsPerPage={PAGE_SIZE}"]
        if self.company_slug not in ("*", "all", ""):
            # WTTJ exposes organization.slug as the filter key (e.g. `auchan`)
            params.append(f"filters=organization.slug%3A{self.company_slug}")
        if cursor_ts is not None:
            # Use `<=` (inclusive) and dedupe via seen-set; strict `<` skips
            # boundary jobs that share a timestamp with the cursor.
            params.append(f"numericFilters=published_at_timestamp%3C%3D{cursor_ts}")
        # For full-platform walk we need the sorted replica so the cursor strategy
        # is monotonic; per-org filtering doesn't need sort and uses the main idx.
        target_url = (
            self._sorted_url
            if self.company_slug in ("*", "all", "") and cursor_ts is not None
            else self._url
        )
        if self.company_slug in ("*", "all", "") and cursor_ts is None:
            # First page of a full walk also benefits from the sorted replica
            target_url = self._sorted_url
        request: dict[str, Any] = {"params": "&".join([*params, f"page={page}"])}
        try:
            response = client.post(target_url, headers=HEADERS, json=request)
        except httpx.HTTPError as exc:
            raise ScraperError(f"WTTJ Algolia call failed: {exc}") from exc
        if response.status_code != 200:
            raise ScraperError(
                f"WTTJ Algolia returned {response.status_code}: {response.text[:120]}"
            )
        return response.json().get("hits") or []

    def _parse_hit(self, hit: dict[str, Any]) -> Job:
        org = hit.get("organization") or {}
        org_name = org.get("name") if isinstance(org, dict) else str(org)
        org_ref = org.get("reference") if isinstance(org, dict) else self.company_slug

        offices = hit.get("offices") or []
        first_office = offices[0] if offices and isinstance(offices[0], dict) else {}
        location_parts = [
            first_office.get("city"),
            first_office.get("state"),
            first_office.get("country"),
        ]
        location = ", ".join(p for p in location_parts if p) or None

        geoloc = (hit.get("_geoloc") or [{}])[0] if hit.get("_geoloc") else {}
        lat = geoloc.get("lat") if isinstance(geoloc, dict) else None
        lon = geoloc.get("lng") if isinstance(geoloc, dict) else None

        salary_min = _to_float(hit.get("salary_minimum"))
        salary_max = _to_float(hit.get("salary_maximum"))
        # Fall back to the yearly-normalized values when only one is set.
        if salary_min is None:
            salary_min = _to_float(hit.get("salary_yearly_minimum"))

        sectors = hit.get("sectors") or []
        department = sectors[0].get("name") if sectors and isinstance(sectors[0], dict) else None

        slug = hit.get("slug") or hit.get("objectID", "")
        url = f"https://www.welcometothejungle.com/{self.language}/companies/{org_ref}/jobs/{slug}"

        raw: dict[str, Any] = {}
        for k in ("contract_type", "remote", "education_level",
                  "languages", "profession", "sector", "tags",
                  "office_distribution", "telework"):
            v = hit.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=hit.get("name") or "Untitled",
            company=org_name or self.company_slug,
            ats_type=ATSType.WELCOMETOTHEJUNGLE,
            ats_id=hit.get("reference") or hit.get("objectID", ""),
            location=location,
            lat=lat,
            lon=lon,
            is_remote=_remote_to_bool(hit.get("remote")),
            salary_currency=hit.get("salary_currency"),
            salary_period=SALARY_PERIOD_MAP.get(hit.get("salary_period") or "yearly", "YEAR"),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_summary=_compose_salary_summary(salary_min, salary_max, hit.get("salary_currency")),
            experience=_to_int(hit.get("experience_level_minimum")),
            employment_type=CONTRACT_TYPE_MAP.get(hit.get("contract_type") or ""),
            department=department,
            commitment=hit.get("contract_type") if isinstance(hit.get("contract_type"), str) else None,
            requisition_id=hit.get("reference") if isinstance(hit.get("reference"), str) else None,
            description=_compose_description(hit),
            posted_at=_parse_iso(hit.get("published_at")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _remote_to_bool(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in ("full", "fulltime", "full_remote"):
        return True
    if v in ("partial", "punctual", "occasional", "punctually"):
        return True
    if v in ("no", "none", ""):
        return False
    return None


def _to_int(value: object) -> int | None:
    f = _to_float(value)
    return int(round(f)) if f is not None else None


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _compose_salary_summary(min_v: float | None, max_v: float | None, ccy: str | None) -> str | None:
    if min_v is None and max_v is None:
        return None
    ccy = ccy or ""
    if min_v is not None and max_v is not None:
        return f"{ccy} {int(min_v):,} - {int(max_v):,}".strip()
    if min_v is not None:
        return f"{ccy} {int(min_v):,}+".strip()
    return f"{ccy} up to {int(max_v):,}".strip() if max_v else None


def _compose_description(hit: dict[str, object]) -> str | None:
    """Build a plain-text description from Algolia's structured fields."""
    parts: list[str] = []
    if isinstance(hit.get("summary"), str) and hit["summary"]:
        parts.append(str(hit["summary"]))
    missions = hit.get("key_missions")
    if isinstance(missions, list) and missions:
        parts.append("\n".join(f"- {m}" for m in missions if isinstance(m, str)))
    profile = hit.get("profile")
    if isinstance(profile, str) and profile:
        parts.append(profile)
    full = "\n\n".join(parts).strip()
    if not full:
        return None
    # Cap at ~25k chars as documented in the Job model
    return full[:25_000]


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if m:
            try:
                return datetime.fromisoformat(m.group(1))
            except ValueError:
                return None
    return None
