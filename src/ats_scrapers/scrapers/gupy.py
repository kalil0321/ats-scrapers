"""Gupy careers scraper.

Gupy is a large Brazilian ATS. Public tenant boards live at
``https://{slug}.gupy.io`` and embed their complete active-job list in the
Next.js ``__NEXT_DATA__`` payload. Job detail pages carry descriptions and
publication timestamps.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

LISTING_TEMPLATE = "https://{slug}.gupy.io/"
DETAIL_TEMPLATE = "https://{slug}.gupy.io/jobs/{job_id}"
DETAIL_CONCURRENCY = 6

_NEXT_DATA_RE = re.compile(
    r"<script\b(?=[^>]*\bid=['\"]__NEXT_DATA__['\"])[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

_EMPLOYMENT_TYPE_MAP = {
    "vacancy_type_effective": "FULL_TIME",
    "vacancy_type_associate": "FULL_TIME",
    "vacancy_type_legal_entity": "CONTRACT",
    "vacancy_legal_entity": "CONTRACT",
    "vacancy_type_outsource": "CONTRACT",
    "vacancy_type_internship": "INTERN",
    "vacancy_type_apprentice": "INTERN",
    "vacancy_type_temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.GUPY)
class GupyScraper(BaseScraper):
    """Scrape one Gupy tenant by subdomain slug."""

    ats = ATSType.GUPY
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            html = await fetch.get_text(
                LISTING_TEMPLATE.format(slug=self.company_slug)
            )
            page_props = self._extract_page_props(html)
            jobs_payload = page_props.get("jobs")
            if not isinstance(jobs_payload, list):
                raise ScraperError(
                    f"Gupy ({self.company_slug}) listing missing jobs array"
                )

            company = _extract_tenant_name(page_props) or self.company_slug
            jobs: list[Job] = []
            seen: set[str] = set()
            for item in jobs_payload:
                if not isinstance(item, dict):
                    continue
                job = self._parse_job(item, company)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)

            if self.include_descriptions and jobs:
                semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(
                    *(
                        self._enrich_description(fetch, semaphore, job)
                        for job in jobs
                    )
                )
            return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                return await self._fetch_description(fetch, str(job.url))

        return self._run_sync(run())

    async def _enrich_description(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with semaphore:
            description = await self._fetch_description(fetch, str(job.url))
        if description:
            job.description = description

    async def _fetch_description(self, fetch: Fetcher, url: str) -> str | None:
        try:
            html = await fetch.get_text(url)
        except ScraperError:
            return None
        page_props = _safe_extract_page_props(html)
        if page_props is None:
            return None
        detail = page_props.get("job")
        if not isinstance(detail, dict):
            return None
        return _compose_description(detail)

    def _extract_page_props(self, html: str) -> dict[str, Any]:
        page_props = _safe_extract_page_props(html)
        if page_props is None:
            raise ScraperError(
                f"Gupy ({self.company_slug}) returned HTML without "
                "a parseable __NEXT_DATA__ block"
            )
        return page_props

    def _parse_job(self, item: dict[str, Any], company: str) -> Job | None:
        raw_id = item.get("id")
        ats_id = str(raw_id).strip() if raw_id is not None else ""
        title = str(item.get("title") or item.get("name") or "").strip()
        if not ats_id or not title:
            return None

        workplace = (
            item.get("workplace")
            if isinstance(item.get("workplace"), dict)
            else {}
        )
        address = (
            workplace.get("address")
            if isinstance(workplace.get("address"), dict)
            else {}
        )
        workplace_type = workplace.get("workplaceType")
        vacancy_type = item.get("type")
        employment_type = (
            _EMPLOYMENT_TYPE_MAP.get(vacancy_type)
            if isinstance(vacancy_type, str)
            else None
        )

        raw: dict[str, Any] = {}
        for key in ("quickApply", "careerPageId", "careerPageUrl", "badges"):
            value = item.get(key)
            if value is not None:
                raw[key] = value
        if isinstance(workplace_type, str) and workplace_type:
            raw["workplace_type"] = workplace_type
        if isinstance(vacancy_type, str) and vacancy_type:
            raw["vacancy_type"] = vacancy_type

        department = item.get("department")
        department = department.strip() if isinstance(department, str) else None
        url = item.get("jobUrl")
        if not isinstance(url, str) or not url.strip():
            url = DETAIL_TEMPLATE.format(slug=self.company_slug, job_id=ats_id)
        elif url.startswith("/"):
            url = f"https://{self.company_slug}.gupy.io{url}"

        posted_at = _parse_iso(item.get("publishedAt"))
        country_iso = _extract_country_iso(address)
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.GUPY,
            ats_id=ats_id,
            location=_format_location(address),
            country_iso=country_iso,
            region="South America" if country_iso == "BR" else None,
            is_remote=_extract_is_remote(workplace_type),
            department=department,
            employment_type=employment_type,
            commitment=vacancy_type if isinstance(vacancy_type, str) else None,
            language="pt",
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _safe_extract_page_props(html: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    props = data.get("props") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        return None
    page_props = props.get("pageProps")
    return page_props if isinstance(page_props, dict) else None


def _extract_tenant_name(page_props: dict[str, Any]) -> str | None:
    career_page = page_props.get("careerPage")
    if not isinstance(career_page, dict):
        return None
    for key in ("publicationName", "name"):
        value = career_page.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return None


def _format_location(address: dict[str, Any]) -> str | None:
    parts = [
        value.strip()
        for key in ("city", "state", "country")
        if isinstance((value := address.get(key)), str) and value.strip()
    ]
    return ", ".join(parts) or None


def _extract_country_iso(address: dict[str, Any]) -> str | None:
    country = address.get("country")
    if not isinstance(country, str) or not country.strip():
        return "BR"
    if country.strip().casefold() in {"brasil", "brazil"}:
        return "BR"
    return None


def _extract_is_remote(workplace_type: object) -> bool | None:
    if not isinstance(workplace_type, str):
        return None
    value = workplace_type.strip().casefold()
    if value == "remote":
        return True
    if value == "on-site":
        return False
    return None


def _compose_description(detail: dict[str, Any]) -> str | None:
    chunks = [
        text
        for key in (
            "description",
            "responsibilities",
            "prerequisites",
            "relevantExperiences",
        )
        if isinstance((value := detail.get(key)), str)
        and (text := _strip_html(value))
    ]
    return "\n\n".join(chunks)[:25_000] or None


def _strip_html(value: str) -> str:
    text = html_mod.unescape(value)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
