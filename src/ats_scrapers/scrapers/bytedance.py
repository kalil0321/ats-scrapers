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

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts"
PAGE_SIZE = 100
MAX_RETRIES = 4

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "regular": "FULL_TIME",
    "permanent": "FULL_TIME",
}

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
                    if job is None or job.ats_id in seen_ids:
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
            url=f"https://joinbytedance.com/search/{ats_id}",
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
            salary_min=_to_float(post_info.get("min_salary")),
            salary_max=_to_float(post_info.get("max_salary")),
            salary_currency=post_info.get("currency"),
            posted_at=_parse_ts(item.get("publish_time") or item.get("post_time")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


def _compose_description(*sources: object) -> str | None:
    """Concatenate description-like fields and cap at 10kB.

    The body sometimes contains repeated whitespace from the API; we
    collapse runs of blank lines to keep storage tight.
    """
    parts: list[str] = []
    for source in sources:
        if isinstance(source, str) and source.strip():
            parts.append(source.strip())
    if not parts:
        return None
    text = "\n\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:10_000] or None


def _extract_label(value: object) -> str | None:
    """ByteDance wraps category-style fields as
    ``{"en_name": "Algorithm", "i18n_name": "Algorithm", ...}``.
    Prefer ``en_name``; fall through to ``i18n_name`` / ``name``."""
    if not isinstance(value, dict):
        return None
    for key in ("en_name", "i18n_name", "name"):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _map_recruit_type(value: object) -> tuple[str | None, str | None]:
    """Map ``recruit_type`` to ``(employment_type, commitment)``.

    The API ships ``{"en_name": "Regular", "i18n_name": "Regular", ...}``.
    We surface the human label in ``commitment`` and translate to the
    canonical FT/PT/CONTRACT/INTERN/TEMPORARY enum.
    """
    label = _extract_label(value)
    if not label:
        return None, None
    norm = label.lower()
    for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
        if needle in norm:
            return mapped, label
    return None, label


def _extract_location(item: dict) -> str | None:
    """ByteDance's `city_info` is a nested location object with parent chain.

    The current API exposes a single `city_info` dict whose `parent` chain
    walks up to country. Legacy `city_list` is handled as a fallback.
    """
    city_info = item.get("city_info")
    if isinstance(city_info, dict):
        parts = []
        node = city_info
        while isinstance(node, dict):
            name = node.get("en_name") or node.get("name")
            if name:
                parts.append(name)
            node = node.get("parent")
        if parts:
            return ", ".join(parts)
    # Legacy: city_list[0].name
    city_list = item.get("city_list") or []
    if city_list and isinstance(city_list[0], dict):
        return city_list[0].get("name")
    return None


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ts(value: int | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OSError):
        return None
