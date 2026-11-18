"""Amazon careers scraper.

Amazon has two job APIs:

1. ``GET https://www.amazon.jobs/en/search.json?result_limit=N&offset=N``
   Public, snake-case payload (``id_icims`` / ``job_path`` /
   ``normalized_location``). **Honors filter query params** like
   ``business_category[]=aws``. Capped at 10,000 results per query —
   bucketing required to exceed it.

2. ``POST https://www.amazon.jobs/api/jobs/search``
   Internal but unauthenticated. Exposes facets with full counts, but
   ``found`` saturates at 10,000 and **its ``filters`` body is silently
   ignored**: every filtered POST returns the unfiltered count. We use
   it only to read the business-category facet; all actual job fetching
   runs through the GET endpoint.

Strategy:

  - POST once with ``size=1`` to read the ``businessCategory`` facet
    (≈62 values, largest ``aws`` ≈ 7K). The facet counts sum to the true
    total (≈21K); ``found`` does not.
  - If ``total < 10K`` → GET-paginate the unfiltered endpoint.
  - Else → for each business category, GET-paginate that bucket. Every
    Amazon business category sits well under 10K so a single layer
    suffices. Confidential postings are counted in the facet but the
    GET endpoint never returns them.

The POST endpoint ignores filters, so bucketing through it (by
``country`` or anything else) returns the same first 10K jobs per bucket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._job_classification import classification_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any, ClassVar

FACET_URL = "https://www.amazon.jobs/api/jobs/search"  # POST — facet discovery only
SEARCH_URL = "https://www.amazon.jobs/en/search.json"   # GET — actual job fetching
PAGE_SIZE = 100
PAGINATION_CAP = 10_000  # Amazon stops returning hits past offset+limit = 10K.
MAX_CONCURRENCY = 6

log = logging.getLogger(__name__)


@ScraperRegistry.register(ATSType.AMAZON)
class AmazonScraper(BaseScraper):
    """Amazon scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.AMAZON

    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Accept-Encoding": "identity",
    }

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            # Single POST call to read the businessCategory facet. The
            # POST endpoint's ``filters`` body is broken (returns
            # unfiltered counts), so we never use it for actual fetching.
            facets_payload = await fetch.post_json(
                FACET_URL,
                json={"searchType": "JOB_SEARCH", "start": 0, "size": 1, "filters": []},
            )
            categories = _extract_facet_values(
                facets_payload.get("facets") or [], "businessCategory"
            )
            # ``found`` stops at 10,000; the facet counts do not.
            found = int(facets_payload.get("found") or 0)
            total = max(found, sum(count for _, count in categories))
            if total == 0:
                return []

            seen: set[str] = set()
            all_jobs: list[Job] = []
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            def absorb(jobs_payload: list[dict[str, Any]]) -> None:
                for hit in jobs_payload:
                    # Amazon jobs can be open in multiple offices —
                    # ``locations`` is a list. Emit one row per
                    # (job × location) so location-based search and
                    # embeddings match each opening individually.
                    for job in self._parse_hit(hit):
                        if not job.ats_id or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        all_jobs.append(job)

            async def get_page(extra_params: dict[str, str], offset: int) -> int:
                """Absorb one page and return the query's ``hits`` count."""
                async with sem:
                    # 400 means "past the pagination cap" — treat as an
                    # empty page so the caller stops.
                    response = await fetch.request(
                        "GET",
                        SEARCH_URL,
                        params={**extra_params, "result_limit": PAGE_SIZE, "offset": offset},
                        handled={400},
                    )
                payload = {} if response.status_code == 400 else response.json()
                absorb(payload.get("jobs") or [])
                return int(payload.get("hits") or 0)

            async def paginate(extra_params: dict[str, str]) -> None:
                hits = await get_page(extra_params, 0)
                if hits > PAGINATION_CAP:
                    log.warning(
                        "Amazon: %s matches %d jobs, only the first %d are reachable",
                        extra_params or "the unfiltered search", hits, PAGINATION_CAP,
                    )
                offsets = range(PAGE_SIZE, min(hits, PAGINATION_CAP), PAGE_SIZE)
                await asyncio.gather(*(get_page(extra_params, o) for o in offsets))

            if total < PAGINATION_CAP or not categories:
                await paginate({})
                return all_jobs

            # Past the cap — bucket by businessCategory. The POST facet uses
            # the same lowercase-dashed slugs that the GET endpoint accepts
            # in ``business_category[]``.
            await asyncio.gather(*(
                paginate({"business_category[]": name}) for name, _ in categories
            ))
            return all_jobs

    def _parse_hit(self, hit: dict[str, Any]) -> list[Job]:
        """Yield one ``Job`` per (Amazon job × posted location).

        Amazon's GET endpoint returns snake_case keys
        (``id_icims`` / ``job_path`` / ``posted_date`` / ``job_schedule_type``);
        the older POST endpoint used camelCase aliases. Read both so an API
        flip-flop doesn't silently empty the row.

        Multi-location: ``locations`` is a list of *JSON-encoded strings*
        (not dicts). Decode each, dedupe by ``normalizedLocation``, and
        emit a row per office so a candidate searching "Vancouver" still
        finds the Seattle/Vancouver dual posting.
        """
        # POST-endpoint payloads wrap data in ``hit.fields`` (each value
        # an array). GET-endpoint payloads are flat. Handle both.
        fields = hit.get("fields") if isinstance(hit, dict) else None
        if isinstance(fields, dict):
            item = {
                k: (v[0] if isinstance(v, list) and v else v)
                for k, v in fields.items()
            }
        else:
            item = hit if isinstance(hit, dict) else {}

        # ats_id: prefer Amazon's ICIMS requisition number (the public
        # job number visible on every posting). Avoid the opaque internal
        # uuid in ``id`` since it isn't human-meaningful.
        req_id = str(
            item.get("icimsJobId") or item.get("id_icims")
            or item.get("jobCode") or item.get("id") or hit.get("id", "")
        )
        # ``job_path`` is the public job page; ``url_next_step`` is the
        # apply flow, which asks the reader to sign in first.
        next_step = _absolute(item.get("urlNextStep") or item.get("url_next_step"))
        url = (
            _absolute(item.get("job_path") or item.get("jobUrl"))
            or next_step
            or f"https://www.amazon.jobs/en/jobs/{req_id}"
        )
        apply_url = _absolute(item.get("urlNextStepApply")) or next_step or url

        # Description: ``description_short`` is a 200-300 char teaser;
        # ``description`` is the full posting body. Keep the long one when
        # available because embeddings benefit from richer text.
        description = (
            item.get("description")
            or item.get("description_short")
            or item.get("businessJobDescription")
            or None
        )

        # ``commitment`` is free-form and mirrors Amazon's wording
        # (``"Full-time"``, ``"Part-time"``); ``employment_type`` is a
        # strict enum, so map separately.
        schedule = (
            item.get("job_schedule_type") or item.get("scheduleType")
            or item.get("schedule") or None
        )
        employment_type = _map_employment_type(schedule, item)

        # Multi-location decode. Each element is a JSON string per the
        # Amazon shape; older POST shape returns dicts directly.
        locations = _decode_locations(item)

        # Common fields shared across every emitted row.
        company = "Amazon"
        title = item.get("title") or item.get("jobTitle") or "Untitled"
        posted_at = _parse_amazon_date(
            item.get("posted_date") or item.get("postedDate")
            or item.get("createdDate") or item.get("created_date")
        )
        team_label = _extract_team_label(item)
        department = (
            item.get("job_category") or item.get("jobCategory")
            or item.get("teamCategory") or None
        )
        family = classification_label(item.get("job_family") or item.get("jobFamily"))
        if family is None:
            family = classification_label(item.get("job_category") or item.get("jobCategory"))
        primary_location = (
            item.get("normalizedLocation")
            or item.get("normalized_location")
            or item.get("location")
        )
        if primary_location and primary_location not in locations:
            locations = [primary_location, *locations]
        if not locations:
            locations = [primary_location] if primary_location else [None]

        # Stable per-location ats_id so the runner's dedup keeps each
        # office posting. Single-location jobs keep the bare req_id.
        rows: list[Job] = []
        for idx, loc in enumerate(locations):
            ats_id = (
                req_id if (len(locations) == 1 or idx == 0)
                else f"{req_id}@loc{idx}"
            )
            raw: dict[str, Any] = {}
            for src in (
                "business_category", "businessCategory", "job_family",
                "jobFamily", "basic_qualifications",
                "preferred_qualifications", "city", "state", "country_code",
                "country", "is_intern", "is_manager", "team_id",
                "primary_search_label", "updated_time",
            ):
                v = item.get(src)
                if v not in (None, "", []):
                    raw[src] = v
            if len(locations) > 1:
                raw["all_locations"] = [loc for loc in locations if loc]
                raw["location_index"] = idx

            rows.append(Job(
                url=url,
                title=title,
                company=company,
                ats_type=ATSType.AMAZON,
                ats_id=ats_id,
                location=loc,
                department=department,
                job_category=family,
                team=team_label,
                description=description,
                commitment=schedule,
                employment_type=employment_type,
                requisition_id=req_id or None,
                apply_url=apply_url if apply_url != url else None,
                posted_at=posted_at,
                fetched_at=datetime.now(UTC),
                raw=raw or None,
            ))
        return rows


def _absolute(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return path if path.startswith("http") else f"https://www.amazon.jobs{path}"


def _extract_facet_values(facets: list[dict[str, Any]], field: str) -> list[tuple[str, int]]:
    for facet in facets:
        if isinstance(facet, dict) and facet.get("name") == field:
            return [
                (v.get("name", ""), int(v.get("count") or 0))
                for v in facet.get("values") or []
                if isinstance(v, dict) and v.get("name") and (v.get("count") or 0) > 0
            ]
    return []


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Amazon prints ``posted_date`` like ``"May  6, 2026"`` (note the
# double-space when the day-of-month is single-digit). ``%B %d, %Y``
# parses both single- and double-spaced variants because ``%d`` is
# whitespace-tolerant on POSIX strptime.
_AMAZON_DATE_FMT = "%B %d, %Y"


def _parse_amazon_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # Try ISO first (covers ``createdDate`` from the POST endpoint).
    iso = _parse_iso(value)
    if iso is not None:
        return iso
    # Collapse double spaces (``"May  6, 2026"`` → ``"May 6, 2026"``).
    cleaned = re.sub(r"\s+", " ", value.strip())
    try:
        return datetime.strptime(cleaned, _AMAZON_DATE_FMT)
    except ValueError:
        return None


def _decode_locations(item: dict[str, object]) -> list[str]:
    """Return a list of unique ``normalizedLocation`` strings from the
    ``locations`` field. The GET endpoint serialises each entry as a
    JSON string; the POST endpoint returns dicts. Tolerate both."""
    raw_list = item.get("locations")
    if not isinstance(raw_list, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw_list:
        d: dict[str, object] | None = None
        if isinstance(entry, dict):
            d = entry
        elif isinstance(entry, str):
            try:
                parsed = json.loads(entry)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                d = parsed
        if d is None:
            continue
        label = d.get("normalizedLocation") or d.get("location")
        if isinstance(label, str) and label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _extract_team_label(item: dict[str, object]) -> str | None:
    """Amazon publishes ``team`` two ways:
    - ``job_family`` / ``jobFamily`` — a string label like
      ``"Real Estate/Facilities"``.
    - ``team`` — a dict whose ``label`` field is the same string but with
      richer metadata.

    Pick the string form when present; otherwise dig into the dict."""
    s = item.get("job_family") or item.get("jobFamily")
    if isinstance(s, str) and s.strip():
        return s.strip()
    team = item.get("team")
    if isinstance(team, dict):
        label = team.get("label") or team.get("title")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def _map_employment_type(
    schedule: object, item: dict[str, object],
) -> str | None:
    """Coerce Amazon's free-form schedule into the Job model's strict
    enum. ``is_intern`` short-circuits to ``INTERN`` regardless of the
    schedule string."""
    if item.get("is_intern"):
        return "INTERN"
    if not isinstance(schedule, str):
        return None
    s = schedule.lower()
    if "full" in s:
        return "FULL_TIME"
    if "part" in s:
        return "PART_TIME"
    if "contract" in s or "fixed" in s:
        return "CONTRACT"
    if "intern" in s:
        return "INTERN"
    if "temp" in s:
        return "TEMPORARY"
    return None


