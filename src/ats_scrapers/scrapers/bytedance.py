"""ByteDance / joinbytedance.com careers scraper.

    POST https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts

Requires ``website-path: en`` plus the standard origin/referer headers
from ``https://joinbytedance.com``; otherwise the endpoint refuses with
400.

This is the same ATSx/Throne SaaS backend that powers TikTok's
``api.lifeattiktok.com``, just a different tenant — schema is identical.
We concatenate ``description`` + ``requirement`` for the canonical
description, map ``recruit_type.en_name`` to the employment-type enum,
and walk ``city_info.parent`` for the location string.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._throne import (
    compose_description as _compose_description,
)
from ats_scrapers.scrapers._throne import (
    extract_label as _extract_label,
)
from ats_scrapers.scrapers._throne import (
    extract_location as _extract_location,
)
from ats_scrapers.scrapers._throne import (
    map_recruit_type as _map_recruit_type,
)
from ats_scrapers.scrapers._throne import (
    to_float as _to_float,
)
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts"
PAGE_SIZE = 100
MAX_RETRIES = 4

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US",
    "content-type": "application/json",
    "website-path": "en",
    "origin": "https://joinbytedance.com",
    "referer": "https://joinbytedance.com/",
    "user-agent": "Mozilla/5.0",
}


@ScraperRegistry.register(ATSType.BYTEDANCE)
class BytedanceScraper(BaseScraper):
    """ByteDance scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.BYTEDANCE
    default_headers: ClassVar[dict[str, str]] = HEADERS

    async def afetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        reported_total: int | None = None
        offset = 0
        async with self.make_fetcher(retries=MAX_RETRIES) as fetch:
            while True:
                payload = {
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "keyword": "",
                    "category_id_list": [],
                    "subject_id_list": [],
                    "location_code_list": [],
                    "job_function_id_list": [],
                }
                body = await self._post_page(fetch, payload)
                code = body.get("code")
                if code:
                    raise ScraperError(
                        f"ByteDance API error code {code}: {body.get('message')}"
                    )
                payload_data = body.get("data")
                if not isinstance(payload_data, dict):
                    raise ScraperError("ByteDance response omitted data object")
                if not isinstance(payload_data.get("job_post_list"), list):
                    raise ScraperError(
                        "ByteDance response omitted job_post_list"
                    )
                total = payload_data.get("count")
                if not isinstance(total, int) or total < 0:
                    raise ScraperError("ByteDance response had invalid count")
                if reported_total is None:
                    reported_total = total
                elif total != reported_total:
                    raise ScraperError(
                        "ByteDance response changed count during pagination "
                        f"({reported_total} to {total})"
                    )
                jobs = payload_data["job_post_list"]
                if not all(isinstance(job, dict) for job in jobs):
                    raise ScraperError(
                        "ByteDance job_post_list contained a non-object row"
                    )
                if not jobs:
                    if not all_jobs:
                        raise ScraperError(
                            "ByteDance full-catalogue scrape returned no jobs"
                        )
                    if offset < total:
                        raise ScraperError(
                            "ByteDance returned an empty page before count "
                            f"was reached ({offset}/{total})"
                        )
                    break
                parsed = [self._parse_job(job) for job in jobs]
                if any(job is None for job in parsed):
                    raise ScraperError(
                        "ByteDance could not parse every returned job row"
                    )
                for job in parsed:
                    if (
                        job is None
                        or not job.ats_id
                        or job.ats_id in seen_ids
                    ):
                        continue
                    seen_ids.add(job.ats_id)
                    all_jobs.append(job)
                offset += len(jobs)
                if offset >= total:
                    break
                if len(jobs) < PAGE_SIZE:
                    raise ScraperError(
                        "ByteDance returned a short page before count "
                        f"was reached ({offset}/{total})"
                    )
        if reported_total is None or len(seen_ids) != reported_total:
            raise ScraperError(
                "ByteDance catalogue ended before the reported count "
                f"({len(seen_ids)}/{reported_total} unique jobs)"
            )
        return all_jobs

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    async def _post_page(
        self,
        fetch: Fetcher,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        body = await fetch.post_json(API_URL, json=payload)
        if not isinstance(body, dict):
            raise ScraperError(
                "ByteDance returned a non-object JSON response"
            )
        return body

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "")
        title = (item.get("title") or item.get("name") or "").strip()
        if not ats_id or not title:
            return None
        post_info = item.get("job_post_info") or {}
        salary_min = _to_float(post_info.get("min_salary"))
        salary_max = _to_float(post_info.get("max_salary"))

        # Description: concatenate ``description`` + ``requirement``
        # (the API splits the body into two fields). Strip and cap.
        description = _compose_description(
            item.get("description"),
            item.get("requirement"),
        )

        # ``recruit_type.en_name`` is the canonical employment-type label
        # ("Intern" / "Regular" / "Contract") — map to our enum.
        employment_type, commitment = _map_recruit_type(item.get("recruit_type"))

        # ``job_category.en_name`` is the high-level area
        # ("Algorithm" / "Engineering"); ``job_subject.en_name`` is the
        # team/role family ("PhD Graduates- 2026 Start", etc.).
        department = _extract_label(item.get("job_category"))
        team = _extract_label(item.get("job_subject"))

        # Use the employer-set ``code`` (e.g. "A72890A") as the
        # requisition id when present; fall back to the numeric ats_id.
        requisition_id = (
            item["code"].strip()
            if isinstance(item.get("code"), str) and item["code"].strip()
            else (ats_id or None)
        )

        raw: dict[str, Any] = {}
        for k in ("job_category", "job_subject", "recruit_type",
                  "experience", "department_info", "skill_list",
                  "tag_list", "process_type"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=HttpUrl(f"https://joinbytedance.com/search/{ats_id}"),
            title=title,
            company="ByteDance",
            ats_type=ATSType.BYTEDANCE,
            ats_id=ats_id,
            location=_extract_location(item),
            department=department,
            team=team if team and team != department else None,
            employment_type=employment_type,
            commitment=commitment,
            description=description if self.include_descriptions else None,
            requisition_id=requisition_id,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=post_info.get("currency"),
            salary_period=(
                "YEAR"
                if salary_min is not None or salary_max is not None
                else None
            ),
            posted_at=_parse_ts(item.get("publish_time") or item.get("post_time")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


def _parse_ts(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OSError):
        return None
