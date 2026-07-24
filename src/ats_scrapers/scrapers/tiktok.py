"""TikTok / Life@TikTok careers scraper.

    POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts

Requires `website-path: tiktok` and origin/referer headers; otherwise the
endpoint refuses with 400.

The API returns rich per-post data (description, requirement,
recruit_type, job_category, job_subject, city_info, salary range).
We concatenate ``description`` + ``requirement`` for the canonical
description, map ``recruit_type.en_name`` to the employment-type enum,
and pull ``job_category.en_name`` as the department.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    from typing import Any, ClassVar

API_URL = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
PAGE_SIZE = 100

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US",
    "content-type": "application/json",
    "website-path": "tiktok",
    "origin": "https://lifeattiktok.com",
    "referer": "https://lifeattiktok.com/",
    "user-agent": "Mozilla/5.0",
}


@ScraperRegistry.register(ATSType.TIKTOK)
class TikTokScraper(BaseScraper):
    """TikTok scraper — `company_slug` is informational; jobs are global."""

    ats = ATSType.TIKTOK

    default_headers: ClassVar[dict[str, str]] = HEADERS

    async def afetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        offset = 0
        async with self.make_fetcher() as fetch:
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
                payload_data = (
                    await fetch.post_json(API_URL, json=payload)
                ).get("data") or {}
                jobs = payload_data.get("job_post_list") or []
                if not jobs:
                    break
                all_jobs.extend(self._parse_job(j) for j in jobs)
                total = payload_data.get("count", 0)
                offset += len(jobs)
                if offset >= total or len(jobs) < PAGE_SIZE:
                    break
        return all_jobs

    def _parse_job(self, item: dict[str, Any]) -> Job:
        ats_id = str(item.get("id") or "")
        post_info = item.get("job_post_info") or {}

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
        # ("Operations" / "Engineering"); ``job_subject.en_name`` is the
        # team/role family ("Project Intern" / "Software Engineer").
        department = _extract_label(item.get("job_category"))
        team = _extract_label(item.get("job_subject"))

        # Use the employer-set ``code`` (e.g. "A205131") as the
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
            url=f"https://lifeattiktok.com/search/{ats_id}",
            title=item.get("title") or item.get("name") or "Untitled",
            company="TikTok",
            ats_type=ATSType.TIKTOK,
            ats_id=ats_id,
            location=_extract_location(item),
            department=department,
            team=team if team and team != department else None,
            employment_type=employment_type,
            commitment=commitment,
            description=description,
            requisition_id=requisition_id,
            salary_min=_to_float(post_info.get("min_salary")),
            salary_max=_to_float(post_info.get("max_salary")),
            salary_currency=post_info.get("currency"),
            posted_at=_parse_ts(item.get("publish_time") or item.get("post_time")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _parse_ts(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value)
    except (ValueError, OSError):
        return None
