"""HERP Hire scraper.

Every public HERP Hire tenant exposes its active jobs on one page:

    GET https://herp.careers/v1/{company_id}

The listing page carries stable job IDs, titles, summaries, and job groups.
Each public detail page additionally exposes schema.org ``JobPosting`` JSON-LD
with the complete description, location, and publication date:

    GET https://herp.careers/v1/{company_id}/{job_id}

No account, API key, browser, or JavaScript rendering is required.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

BASE_URL = "https://herp.careers/v1/{slug}"
DETAIL_URL = f"{BASE_URL}/{{job_id}}"
DETAIL_CONCURRENCY = 8
_DETAIL_HANDLED = frozenset({404, 410})
_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "正社員": "FULL_TIME",
    "パート": "PART_TIME",
    "アルバイト": "PART_TIME",
    "契約社員": "CONTRACT",
    "業務委託": "CONTRACT",
    "派遣社員": "TEMPORARY",
    "インターン": "INTERN",
}
_JAPAN_MARKERS = (
    "日本",
    "Japan",
    "北海道",
    "青森県",
    "岩手県",
    "宮城県",
    "秋田県",
    "山形県",
    "福島県",
    "茨城県",
    "栃木県",
    "群馬県",
    "埼玉県",
    "千葉県",
    "東京都",
    "神奈川県",
    "新潟県",
    "富山県",
    "石川県",
    "福井県",
    "山梨県",
    "長野県",
    "岐阜県",
    "静岡県",
    "愛知県",
    "三重県",
    "滋賀県",
    "京都府",
    "大阪府",
    "兵庫県",
    "奈良県",
    "和歌山県",
    "鳥取県",
    "島根県",
    "岡山県",
    "広島県",
    "山口県",
    "徳島県",
    "香川県",
    "愛媛県",
    "高知県",
    "福岡県",
    "佐賀県",
    "長崎県",
    "熊本県",
    "大分県",
    "宮崎県",
    "鹿児島県",
    "沖縄県",
)
_REMOTE_RE = re.compile(r"(?:full[ -]?remote|remote|フルリモート|完全在宅)", re.I)


@ScraperRegistry.register(ATSType.HERP)
class HerpScraper(BaseScraper):
    """Scrape one HERP Hire company ID."""

    ats = ATSType.HERP

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = require_host_label(company_slug, provider="HerpScraper")

    async def afetch(self) -> list[Job]:
        url = BASE_URL.format(slug=self.company_slug)
        async with self.make_fetcher() as fetch:
            html = await fetch.get_text(url)
            jobs = self._parse_listing(html)
            if self.include_descriptions and jobs:
                semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(self._enrich_detail(fetch, semaphore, job) for job in jobs))
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich_detail(fetch, asyncio.Semaphore(1), job)
            return job.description

        return self._run_sync(run())

    def _parse_listing(self, html: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one(".requisition-list") is None:
            raise ScraperError(f"HERP ({self.company_slug}) returned an unrecognized careers page")

        company_meta = soup.select_one('meta[property="og:site_name"]')
        company = (
            company_meta.get("content", "").strip() if company_meta is not None else ""
        ) or self.company_slug

        jobs: list[Job] = []
        seen: set[str] = set()
        path_pattern = re.compile(rf"/v1/{re.escape(self.company_slug)}/([^/?#]+)")
        for card in soup.select(".requisition-list-card"):
            anchor = card.select_one(".requisition-list-card__header-anchor[href]")
            if anchor is None:
                continue
            match = path_pattern.fullmatch(str(anchor.get("href") or ""))
            if not match:
                continue
            job_id = match.group(1)
            if job_id in seen:
                continue
            title_node = card.select_one("h2")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            if not title:
                continue
            seen.add(job_id)

            summary_node = card.select_one(".requisition-list-card__text")
            summary = (
                summary_node.get_text("\n", strip=True)[:25_000]
                if summary_node is not None
                else None
            )
            groups = [
                node.get_text(" ", strip=True)
                for node in card.select(".career-page-group-name-tag__text")
                if node.get_text(" ", strip=True)
            ]
            job_url = DETAIL_URL.format(
                slug=self.company_slug,
                job_id=job_id,
            )
            jobs.append(
                Job(
                    url=job_url,
                    title=title,
                    company=company,
                    ats_type=ATSType.HERP,
                    ats_id=job_id,
                    department="; ".join(dict.fromkeys(groups)) or None,
                    apply_url=f"{job_url}/apply",
                    description=summary if self.include_descriptions else None,
                    fetched_at=datetime.now(UTC),
                )
            )
        return jobs

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with semaphore:
            try:
                response = await fetch.request(
                    "GET",
                    str(job.url),
                    handled=_DETAIL_HANDLED,
                )
            except ScraperError:
                return
        if response.status_code != 200:
            return
        detail = _job_posting_json_ld(response.text)
        if detail is not None:
            _apply_detail(job, detail)


def _job_posting_json_ld(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        candidates: list[Any]
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            candidates = payload["@graph"]
        else:
            candidates = [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _apply_detail(job: Job, detail: dict[str, Any]) -> None:
    description = detail.get("description")
    if isinstance(description, str) and description.strip():
        description = description.strip()[:25_000]
        if not job.description or len(description) > len(job.description):
            job.description = description

    location = _location_text(detail.get("jobLocation"))
    if location:
        job.location = location
        if any(marker.lower() in location.lower() for marker in _JAPAN_MARKERS):
            job.country_iso = "JP"
            job.region = "Asia"
        if _REMOTE_RE.search(location):
            job.is_remote = True

    posted_at = _parse_datetime(detail.get("datePosted"))
    if posted_at is not None:
        job.posted_at = posted_at

    employment = detail.get("employmentType")
    if isinstance(employment, list):
        employment = next((value for value in employment if isinstance(value, str)), None)
    if isinstance(employment, str) and employment.strip():
        label = employment.strip()
        job.commitment = label
        job.employment_type = _EMPLOYMENT_TYPE_MAP.get(label.upper()) or next(
            (mapped for marker, mapped in _EMPLOYMENT_TYPE_MAP.items() if marker in label),
            None,
        )


def _location_text(value: object) -> str | None:
    if isinstance(value, list):
        parts = [_location_text(item) for item in value]
        return "; ".join(dict.fromkeys(part for part in parts if part)) or None
    if not isinstance(value, dict):
        return None
    address = value.get("address")
    if isinstance(address, str):
        return address.strip() or None
    if not isinstance(address, dict):
        return None
    parts = [
        str(address[key]).strip()
        for key in (
            "streetAddress",
            "addressLocality",
            "addressRegion",
            "postalCode",
            "addressCountry",
        )
        if address.get(key)
    ]
    return ", ".join(parts) or None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
