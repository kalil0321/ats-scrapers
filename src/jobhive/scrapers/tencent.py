"""Tencent Careers — official China-first company careers API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://careers.tencent.com/tencentcareer/api/post/Query"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5


@ScraperRegistry.register(ATSType.TENCENT)
class TencentScraper(BaseScraper):
    """Tencent official careers scraper.

    The default China/Chinese surface returned ~2.6k live postings when
    verified on 2026-05-11. Pass ``language="en-us", area="us"`` for
    Tencent's English/global view.
    """

    ats = ATSType.TENCENT

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        language: str = "zh-cn",
        area: str = "cn",
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.page_size = page_size
        self.max_pages = max_pages
        self.language = language
        self.area = area

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            first = await self._fetch_page(client, sem, page=1)
            data = first.get("Data") or {}
            total = int(data.get("Count") or 0)
            jobs = self._parse_posts(data.get("Posts") or [])
            if total <= self.page_size:
                return jobs

            seen = {j.ats_id for j in jobs}
            lock = asyncio.Lock()
            page_count = min(
                (total + self.page_size - 1) // self.page_size,
                self.max_pages,
            )

            async def one(page: int) -> None:
                payload = await self._fetch_page(client, sem, page=page)
                page_jobs = self._parse_posts(
                    (payload.get("Data") or {}).get("Posts") or []
                )
                async with lock:
                    for job in page_jobs:
                        if job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)

            await asyncio.gather(*(one(p) for p in range(2, page_count + 1)))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        params = {
            "timestamp": int(datetime.now().timestamp() * 1000),
            "countryId": "",
            "cityId": "",
            "bgIds": "",
            "productId": "",
            "categoryId": "",
            "parentCategoryId": "",
            "attrId": "",
            "keyword": "",
            "pageIndex": page,
            "pageSize": self.page_size,
            "language": self.language,
            "area": self.area,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL,
                        params=params,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                            "Referer": "https://careers.tencent.com/search.html",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Tencent fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Tencent returned non-JSON at page={page}: {exc}"
                    ) from exc
                if payload.get("Code") != 200:
                    raise ScraperError(
                        f"Tencent returned application code "
                        f"{payload.get('Code')} at page={page}"
                    )
                return payload
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Tencent returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Tencent returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"Tencent exhausted retries at page={page}: {last_exc}")

    def _parse_posts(self, posts: list[dict[str, Any]]) -> list[Job]:
        return [job for post in posts if (job := self._parse_post(post)) is not None]

    def _parse_post(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("PostId") or item.get("RecruitPostId") or "")
        title = (item.get("RecruitPostName") or "").strip()
        if not ats_id or not title:
            return None

        raw: dict[str, Any] = {}
        for source, dest in (
            ("RecruitPostId", "recruit_post_id"),
            ("CountryName", "country"),
            ("BGName", "business_group"),
            ("ProductName", "product"),
            ("CategoryName", "category"),
            ("RequireWorkYearsName", "experience_label"),
            ("LastUpdateTime", "last_update_time"),
        ):
            value = item.get(source)
            if value not in (None, ""):
                raw[dest] = value

        return Job(
            url=(
                item.get("PostURL")
                or f"https://careers.tencent.com/jobdesc.html?postId={ats_id}"
            ),
            title=title,
            company="Tencent",
            ats_type=ATSType.TENCENT,
            ats_id=ats_id,
            location=_join_nonempty(item.get("LocationName"), item.get("CountryName")),
            department=(item.get("CategoryName") or None),
            team=(item.get("ProductName") or item.get("BGName") or None),
            description=_join_nonempty(
                item.get("Responsibility"), item.get("Requirement"), sep="\n\n"
            ),
            posted_at=_parse_date(item.get("LastUpdateTime")),
            fetched_at=datetime.now(),
            language=self.language[:2],
            raw=raw or None,
        )


def _join_nonempty(*values: object, sep: str = ", ") -> str | None:
    parts = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return sep.join(parts) if parts else None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y年%m月%d日", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None
