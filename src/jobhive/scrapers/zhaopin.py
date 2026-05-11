"""Zhaopin / 智联招聘 — China direct job board API."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any


API_URL = "https://fe-api.zhaopin.com/c/i/search/positions"
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 5
MAX_RETRIES = 2

DEFAULT_CITY_CODES = (
    "489",  # Beijing
    "538",  # Shanghai
    "765",  # Shenzhen
    "736",  # Guangzhou
    "530",  # Hangzhou
    "801",  # Chengdu
    "854",  # Wuhan
    "635",  # Nanjing
    "702",  # Suzhou
    "600",  # Tianjin
)

DEFAULT_KEYWORDS = (
    "",
    "工程师",
    "销售",
    "客服",
    "会计",
    "运营",
    "产品",
    "Java",
    "Python",
    "司机",
)


@ScraperRegistry.register(ATSType.ZHAOPIN)
class ZhaopinScraper(BaseScraper):
    """Zhaopin scraper using the official search JSON API.

    `reverse-api-engineer` found the SPA's real endpoint on 2026-05-11:
    `POST /c/i/search/positions` with a plain JSON body. The older `sou`
    endpoint is CAPTCHA-gated, but this positions endpoint works without
    cookies from a normal HTTP client.
    """

    ats = ATSType.ZHAOPIN

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        city_codes: Iterable[str] = DEFAULT_CITY_CODES,
        keywords: Iterable[str] = DEFAULT_KEYWORDS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.city_codes = tuple(city_codes)
        self.keywords = tuple(keywords)
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.zhaopin.com/",
                "x-zp-business-system": "1",
                "x-zp-page-code": "4019",
                "x-zp-platform": "13",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        ) as client:
            for city_code in self.city_codes:
                for keyword in self.keywords:
                    for page in range(1, self.max_pages + 1):
                        payload = await self._fetch_page(
                            client,
                            city_code=city_code,
                            keyword=keyword,
                            page=page,
                        )
                        data = payload.get("data") or {}
                        items = data.get("list") or []
                        if not items:
                            break
                        for job in self._parse_items(items):
                            if job.ats_id in seen:
                                continue
                            seen.add(job.ats_id)
                            jobs.append(job)
                        if data.get("isEndPage"):
                            break
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        *,
        city_code: str,
        keyword: str,
        page: int,
    ) -> dict[str, Any]:
        action_id = str(uuid.uuid4())
        body = {
            "S_SOU_FULL_INDEX": keyword,
            "S_SOU_WORK_CITY": city_code,
            "order": 4,
            "actionid": action_id,
            "pageSize": self.page_size,
            "pageIndex": page,
            "eventScenario": "pcSearchedSouSearch",
            "anonymous": 1,
            "clickFilterBlackCompany": False,
            "platform": 13,
            "version": "0.0.0",
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(
                    API_URL,
                    json=body,
                    headers={"x-zp-action-id": action_id},
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Zhaopin fetch failed city={city_code} "
                        f"keyword={keyword!r} page={page}: {exc}"
                    ) from exc
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Zhaopin returned invalid JSON city={city_code} "
                        f"keyword={keyword!r} page={page}: {exc}"
                    ) from exc
                if payload.get("code") != 200:
                    raise ScraperError(
                        f"Zhaopin application code {payload.get('code')} "
                        f"city={city_code} keyword={keyword!r} page={page}"
                    )
                return payload
            if attempt == MAX_RETRIES:
                raise ScraperError(
                    f"Zhaopin returned HTTP {response.status_code} "
                    f"city={city_code} keyword={keyword!r} page={page}"
                )
        raise ScraperError(
            f"Zhaopin exhausted retries city={city_code} keyword={keyword!r} "
            f"page={page}: {last_exc}"
        )

    def _parse_items(self, items: list[dict[str, Any]]) -> list[Job]:
        return [job for item in items if (job := self._parse_item(item)) is not None]

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        detail = item.get("jobDetailData") or {}
        position = (detail.get("position") or {}) if isinstance(detail, dict) else {}
        base = (position.get("base") or {}) if isinstance(position, dict) else {}
        desc = (position.get("desc") or {}) if isinstance(position, dict) else {}
        location = (position.get("workLocation") or {}) if isinstance(position, dict) else {}

        ats_id = str(
            base.get("positionNumber") or item.get("positionNumber") or item.get("number") or ""
        ).strip()
        title = _clean(base.get("positionName")) or _clean(item.get("positionName"))
        if not ats_id or not title:
            return None

        salary_summary = _clean(item.get("salary60")) or _clean(base.get("salary"))
        salary_min, salary_max = _parse_salary(salary_summary)
        company = _clean(item.get("companyName")) or "Zhaopin employer"
        location_text = (
            _clean(location.get("address"))
            or _location_from_card(item.get("cardCustomJson"))
            or _clean(item.get("cityDistrict"))
        )
        raw = {
            key: value
            for key, value in {
                "city_id": item.get("cityId"),
                "company_number": item.get("companyNumber"),
                "company_size": item.get("companySize"),
                "education": item.get("education") or base.get("education"),
                "industry": item.get("industryName"),
                "work_type": base.get("workType"),
                "labels": desc.get("labels"),
                "card_custom_json": item.get("cardCustomJson"),
            }.items()
            if value not in (None, "", [], {})
        }

        return Job(
            url=_clean(item.get("positionURL"))
            or _clean(item.get("positionUrl"))
            or f"https://www.zhaopin.com/jobdetail/{ats_id}.htm",
            title=title,
            company=company,
            ats_type=ATSType.ZHAOPIN,
            ats_id=ats_id,
            location=location_text,
            country_iso="CN",
            salary_currency="CNY" if salary_summary else None,
            salary_period="MONTH" if salary_summary else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=_parse_experience(
                item.get("workingExp")
                or item.get("workExperience")
                or base.get("positionWorkingExp")
            ),
            department=_clean(item.get("industryName")),
            description=_clean(desc.get("description")),
            fetched_at=datetime.now(),
            language="zh",
            raw=raw or None,
        )


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _location_from_card(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r'"address"\s*:\s*"([^"]+)"', value)
    return match.group(1) if match else None


def _parse_salary(summary: str | None) -> tuple[float | None, float | None]:
    if not summary:
        return None, None
    match = re.search(r"(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?\s*(千|万)", summary)
    if not match:
        return None, None
    unit = 1000 if match.group(3) == "千" else 10000
    min_amount = float(match.group(1)) * unit
    max_amount = float(match.group(2)) * unit if match.group(2) else None
    return min_amount, max_amount


def _parse_experience(value: object) -> int | None:
    text = _clean(value)
    if not text:
        return None
    if "不限" in text or "应届" in text or "在校" in text:
        return 0
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None
