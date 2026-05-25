"""Apple careers scraper.

Apple's job board requires a CSRF token before search calls succeed:

    1. GET https://jobs.apple.com/api/v1/CSRFToken     # cookie + header set
    2. POST https://jobs.apple.com/api/v1/jobsTeam     # search payload

The CSRF flow is held in a single httpx.Client session.

Description completeness: the search API only exposes ``jobSummary``
(the intro paragraph, ~500–1000 chars). The full posting body —
``description``, ``minimumQualifications``, ``preferredQualifications``
— lives in the React loader state embedded on each job's detail page
(``window.__loaderData__`` JSON). After collecting search results, we
fetch each detail page concurrently and assemble the full description.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
DETAIL_CONCURRENCY = 25
DETAIL_TIMEOUT_S = 10.0
# Marker we walk forward from to extract the JS string literal passed
# to ``JSON.parse``. Regex non-greedy ``"(.+?)"`` would truncate any
# payload containing the byte sequence ``")`` inside an escaped string
# (e.g. ``\"OKRs\")`` in job copy), so we instead scan the JS string
# character-by-character respecting backslash escapes.
_LOADER_PREFIX = 'JSON.parse("'

_LOG = logging.getLogger(__name__)


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
                # Apple's catalog (~5 k jobs / 250 pages) means a single
                # mid-fetch ``ReadTimeout`` or transient 5xx must not
                # discard the dozens of pages already accumulated. Retry
                # transient failures with exponential backoff; if all
                # retries are exhausted, log a warning and break out
                # of the pagination loop, returning ``all_jobs`` so far.
                response: httpx.Response | None = None
                last_exc: Exception | None = None
                last_status: int | None = None
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        r = client.post(SEARCH_URL, json=payload)
                    except httpx.HTTPError as exc:
                        last_exc = exc
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                        continue
                    if r.status_code == 200:
                        response = r
                        break
                    last_status = r.status_code
                    if r.status_code == 429 or 500 <= r.status_code < 600:
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                        continue
                    raise ScraperError(
                        f"Apple search returned {r.status_code}: {r.text[:120]}"
                    )
                if response is None:
                    # When page 1 exhausts retries we have no partial
                    # data — returning ``[]`` would silently masquerade
                    # as a successful zero-result scrape and let cron
                    # / downstream consumers treat an outage as
                    # "Apple has no jobs today." Only the
                    # partial-result fallback (page ≥ 2) is safe; for
                    # page 1 raise so the failure surfaces as a non-
                    # zero exit code.
                    if not all_jobs:
                        raise ScraperError(
                            f"Apple search page {page} failed after "
                            f"{MAX_RETRIES} retries (last_status="
                            f"{last_status} last_exc={last_exc})"
                        )
                    _LOG.warning(
                        "Apple search page %d failed after %d retries "
                        "(last_status=%s last_exc=%s); returning %d "
                        "partial jobs",
                        page, MAX_RETRIES, last_status, last_exc,
                        len(all_jobs),
                    )
                    break
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

        # Detail-page enrichment: pull the full body (description +
        # min/preferred qualifications) from each job's React loader
        # state. Best-effort — a failed detail fetch keeps the job's
        # listing-level ``jobSummary`` instead.
        if self.include_descriptions and all_jobs:
            try:
                asyncio.run(_enrich_apple_details(all_jobs, self.timeout))
            except Exception as exc:  # pragma: no cover - defensive
                _LOG.warning("Apple detail enrichment failed: %s", exc)

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


# ---------------------------------------------------------------------------
# Detail-page enrichment
# ---------------------------------------------------------------------------


async def _enrich_apple_details(jobs: list[Job], timeout_s: float) -> None:
    """Concurrent fetch of each job's detail page, replacing ``description``
    with the full body assembled from the React loader state.

    The detail HTML embeds:

        window.__loaderData__ = JSON.parse("…escaped JSON…");

    which contains ``loaderData.jobDetails.jobsData.localizations.en_US.posting``
    with fields ``jobSummary`` / ``description`` / ``minimumQualifications``
    / ``preferredQualifications``. We concatenate them under markdown
    headings so the post-scrape markdownify pass produces a clean,
    section-headered description (~3-5× longer than the search summary).
    """
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    seen_positions: set[str] = set()
    # Position ids whose detail page was successfully parsed and whose
    # corresponding job row got its description replaced with the
    # assembled long body. Used in the broadcast pass below to tell the
    # hydrated long description apart from the short ``jobSummary`` that
    # sibling rows (same posting, different location) inherited from
    # the search payload.
    hydrated_positions: set[str] = set()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(DETAIL_TIMEOUT_S, connect=4.0),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
    ) as client:

        async def hydrate(job: Job) -> None:
            # Multi-location jobs share one detail page — fetch each
            # position once and broadcast the result to all of its
            # location-specific Job rows further below.
            position_id = (job.requisition_id or "").split("@")[0]
            if not position_id:
                return
            if position_id in seen_positions:
                return
            seen_positions.add(position_id)
            async with sem:
                try:
                    r = await client.get(str(job.url))
                except (httpx.HTTPError, OSError):
                    return
            if r.status_code != 200:
                return
            posting = _extract_apple_posting(r.text)
            if not posting:
                return
            description = _assemble_apple_description(posting)
            if description:
                job.description = description[:25_000]
                hydrated_positions.add(position_id)

        await asyncio.gather(
            *(hydrate(j) for j in jobs),
            return_exceptions=True,
        )

        # Second pass: broadcast each hydrated long description to every
        # sibling row of the same position. The initial ``jobSummary``
        # those siblings inherited from search is shorter (often ~500
        # chars) than the assembled detail body (~2-5k chars), so we
        # always overwrite — gated on ``hydrated_positions`` so a
        # detail fetch that failed doesn't get its sibling's existing
        # ``jobSummary`` wiped or duplicated.
        hydrated_desc_by_position: dict[str, str] = {}
        for j in jobs:
            pid = (j.requisition_id or "").split("@")[0]
            if (
                pid in hydrated_positions
                and j.description
                and pid not in hydrated_desc_by_position
            ):
                hydrated_desc_by_position[pid] = j.description
        for j in jobs:
            pid = (j.requisition_id or "").split("@")[0]
            if pid in hydrated_desc_by_position:
                j.description = hydrated_desc_by_position[pid]


def _extract_js_string_literal(html: str, marker: str) -> str | None:
    """Find ``marker`` in ``html`` and return the JS double-quoted string
    that immediately follows it. Walks character-by-character respecting
    backslash escapes so payloads containing ``\\")`` survive intact.
    Returns the inner (still-escaped) string content without the
    surrounding quotes, or ``None`` if the marker isn't found or the
    string is unterminated.
    """
    start = html.find(marker)
    if start < 0:
        return None
    i = start + len(marker)
    n = len(html)
    body_chars: list[str] = []
    while i < n:
        ch = html[i]
        if ch == "\\":
            # Take the backslash and the following char as a unit.
            if i + 1 >= n:
                return None
            body_chars.append(ch)
            body_chars.append(html[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(body_chars)
        body_chars.append(ch)
        i += 1
    return None


def _extract_apple_posting(html: str) -> dict[str, Any] | None:
    """Pull the ``posting`` object out of the React loader-data JSON."""
    encoded = _extract_js_string_literal(html, _LOADER_PREFIX)
    if encoded is None:
        return None
    try:
        # The loader payload is JSON encoded as a JS string. The JSON
        # decoder itself handles all JS-string escape sequences (\n,
        # \", \uXXXX surrogate pairs, etc.) correctly when fed a quoted
        # string, so we wrap the captured payload in quotes and let
        # json.loads do the unescape. This avoids the lone-surrogate
        # bug that ``codecs.decode(..., "unicode_escape")`` exhibits on
        # supplementary-plane characters (emoji, math symbols, …).
        raw_json = json.loads('"' + encoded + '"')
        data = json.loads(raw_json)
    except (ValueError, json.JSONDecodeError):
        return None

    loader = data.get("loaderData") or {}
    job_details = loader.get("jobDetails") or {}
    jobs_data = job_details.get("jobsData") or {}
    localizations = jobs_data.get("localizations") or {}
    # Prefer en_US, then en_UK, then anything; fall back to top-level
    # jobsData fields if the localizations bundle is missing.
    for key in ("en_US", "en_UK"):
        loc = localizations.get(key)
        if isinstance(loc, dict):
            posting = loc.get("posting") or {}
            if posting:
                return posting
    for loc in localizations.values():
        if isinstance(loc, dict):
            posting = loc.get("posting") or {}
            if posting:
                return posting
    # Last resort — top-level fields
    top = {
        k: jobs_data[k]
        for k in ("jobSummary", "description", "minimumQualifications", "preferredQualifications")
        if k in jobs_data
    }
    return top or None


def _assemble_apple_description(posting: dict[str, Any]) -> str | None:
    """Assemble the markdown-ready description from Apple posting fields."""
    parts: list[str] = []
    summary = (posting.get("jobSummary") or "").strip()
    if summary:
        parts.append(summary)
    body = (posting.get("description") or "").strip()
    if body:
        parts.append("## Description\n\n" + body)
    min_q = (posting.get("minimumQualifications") or "").strip()
    if min_q:
        parts.append("## Minimum qualifications\n\n" + min_q)
    pref_q = (posting.get("preferredQualifications") or "").strip()
    if pref_q:
        parts.append("## Preferred qualifications\n\n" + pref_q)
    if not parts:
        return None
    return "\n\n".join(parts)
