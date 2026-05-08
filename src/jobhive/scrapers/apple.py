"""Apple careers scraper.

Apple's job board requires a CSRF token before search calls succeed:

    1. GET https://jobs.apple.com/api/v1/CSRFToken     # cookie + header set
    2. POST https://jobs.apple.com/api/v1/jobsTeam     # search payload

The CSRF flow is held in a single httpx.Client session.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://jobs.apple.com"
CSRF_URL = f"{BASE_URL}/api/v1/CSRFToken"
SEARCH_URL = f"{BASE_URL}/api/v1/search"
PAGE_SIZE = 20


@ScraperRegistry.register(ATSType.APPLE)
class AppleScraper(BaseScraper):
    """Apple scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.APPLE

    def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            client.headers.update(
                {
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/en-us/search",
                }
            )
            try:
                csrf_response = client.get(CSRF_URL)
            except httpx.HTTPError as exc:
                raise ScraperError(f"Apple CSRF fetch failed: {exc}") from exc
            if csrf_response.status_code != 200:
                raise ScraperError(
                    f"Apple CSRF endpoint returned {csrf_response.status_code}"
                )
            csrf_token = csrf_response.headers.get("x-apple-csrf-token")
            if not csrf_token:
                raise ScraperError("Apple did not return an x-apple-csrf-token header")
            client.headers["X-Apple-CSRF-Token"] = csrf_token

            page = 1
            while True:
                payload = {
                    "query": "",
                    "filters": {},
                    "page": page,
                    "locale": "en-us",
                    "sort": "",
                    "format": {
                        "longDate": "MMMM D, YYYY",
                        "mediumDate": "MMM D, YYYY",
                    },
                }
                try:
                    response = client.post(SEARCH_URL, json=payload)
                except httpx.HTTPError as exc:
                    raise ScraperError(f"Apple search failed: {exc}") from exc
                if response.status_code != 200:
                    raise ScraperError(
                        f"Apple search returned {response.status_code}: {response.text[:120]}"
                    )
                data = response.json()
                postings = (data.get("res") or {}).get("searchResults") or []
                if not postings:
                    break
                for p in postings:
                    all_jobs.extend(self._parse_job(p))
                total = (data.get("res") or {}).get("totalRecords", 0)
                if page * PAGE_SIZE >= total or len(postings) < PAGE_SIZE:
                    break
                page += 1
        return all_jobs

    def _parse_job(self, item: dict[str, Any]) -> list[Job]:
        """Yield one ``Job`` per (Apple posting × location).

        Apple's search returns rich structured data — most fields the
        old scraper dropped. ``team`` is a dict (we want ``teamName``),
        ``postDateInGMT`` is the real ISO timestamp (``postingDate`` is
        the formatted display string), and ``jobSummary`` is the full
        description body. ``homeOffice`` flags fully-remote roles.

        Multi-location: when ``isMultiLocation`` is true (or the
        ``locations`` list has >1 entry), emit one row per location with
        a composite ``ats_id`` so location-based search hits each office.
        """
        req_id = str(item.get("reqId") or item.get("id") or "")
        position_id = str(item.get("positionId") or item.get("id") or "")
        slug = item.get("transformedPostingTitle") or item.get("titleSlug") or "role"
        url = f"{BASE_URL}/en-us/details/{position_id}/{slug}"
        title = item.get("postingTitle") or item.get("title") or "Untitled"

        # Description — full-text body Apple ships in every search hit.
        description = item.get("jobSummary") or None

        # Team is a dict with teamName / teamID / teamCode. The label is
        # the only thing that's user-meaningful for the dataset.
        team = item.get("team")
        team_label: str | None = None
        if isinstance(team, dict):
            team_label = team.get("teamName") or team.get("teamCode")
        elif isinstance(team, str):
            team_label = team

        # Apple ships ``postDateInGMT`` as an ISO timestamp; the
        # ``postingDate`` field is the formatted display string ("May
        # 06, 2026") and never parses as ISO.
        posted_at = (
            _parse_iso(item.get("postDateInGMT"))
            or _parse_iso(item.get("postedDate"))
        )

        # Apple's only schedule signal is ``standardWeeklyHours``.
        # 30+ → full-time; less → part-time.
        hours = item.get("standardWeeklyHours")
        employment_type: str | None = None
        commitment: str | None = None
        if isinstance(hours, (int, float)) and hours > 0:
            commitment = f"{int(hours)}h/week"
            employment_type = "FULL_TIME" if hours >= 30 else "PART_TIME"

        # ``homeOffice`` is Apple's fully-remote flag. Some roles are
        # office-only (False), others remote (True), some neither
        # (None). Don't infer; only set when explicit.
        is_remote = item.get("homeOffice") if isinstance(item.get("homeOffice"), bool) else None

        # Location list — usually 1 entry; multi-location roles can have
        # 2-5. Each entry has a fully-formed ``name`` ("Cupertino,
        # California, United States") plus city/state/country parts.
        locations = _decode_locations(item)
        if not locations:
            locations = [None]

        raw_base: dict[str, Any] = {}
        for k in ("type", "managedPipelineRole", "isMultiLocation",
                  "postExternal", "minimumQualifications",
                  "preferredQualifications", "education",
                  "keyQualifications"):
            v = item.get(k)
            if v not in (None, "", [], False):
                raw_base[k] = v
        if isinstance(team, dict):
            raw_base["team"] = team

        rows: list[Job] = []
        for idx, loc in enumerate(locations):
            ats_id = (
                position_id if (len(locations) == 1 or idx == 0)
                else f"{position_id}@loc{idx}"
            )
            raw = dict(raw_base)
            if len(locations) > 1:
                raw["all_locations"] = [loc for loc in locations if loc]
                raw["location_index"] = idx
            rows.append(Job(
                url=url,
                title=title,
                company="Apple",
                ats_type=ATSType.APPLE,
                ats_id=ats_id,
                location=loc,
                is_remote=is_remote,
                team=team_label,
                description=description,
                employment_type=employment_type,
                commitment=commitment,
                requisition_id=req_id or position_id or None,
                posted_at=posted_at,
                fetched_at=datetime.now(),
                raw=raw or None,
            ))
        return rows


def _decode_locations(item: dict[str, Any]) -> list[str]:
    """Return a deduped list of human-readable location strings.

    Apple's ``locations`` entries look like
    ``{"city": "Cupertino", "stateProvince": "California",
       "countryName": "United States", "name": "Cupertino, California,
       United States"}``.
    The ``name`` field is already nicely formatted, so prefer it; fall
    back to assembling city/state/country when ``name`` is empty.
    """
    locs = item.get("locations") or item.get("locationsList") or []
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(locs, list):
        for entry in locs:
            label: str | None = None
            if isinstance(entry, dict):
                name = (entry.get("name") or "").strip()
                if name:
                    label = name
                else:
                    parts = [
                        (entry.get("city") or "").strip(),
                        (entry.get("stateProvince") or "").strip(),
                        (entry.get("countryName") or "").strip(),
                    ]
                    label = ", ".join(p for p in parts if p) or None
            elif isinstance(entry, str):
                label = entry.strip() or None
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    if not out and isinstance(item.get("location"), str):
        loc = item["location"].strip()
        if loc:
            out.append(loc)
    return out


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
