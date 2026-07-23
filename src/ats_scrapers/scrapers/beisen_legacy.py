"""Beisen's pre-2022 server-rendered zhiye.com careers scraper.

Legacy tenants list jobs under ``/Social``, ``/Campus``, and ``/Intern`` or
the older ``/index`` card layout instead of exposing the current Beisen
``BSGlobal`` JSON API. Listing and detail pages are static HTML, so this
scraper parses the public pages directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

CATEGORIES = ("Social", "Campus", "Intern")
LISTING_PATH = "/{category}/?PageIndex={page}"
INDEX_PATH = "/index/?PageIndex={page}"
DETAIL_PATH = "/zpdetail/{ats_id}"
MAX_PAGES = 500
DETAIL_CONCURRENCY = 6

_JOB_ID_RE = re.compile(r"/zpdetail/(\d+)", re.IGNORECASE)
_PAGE_RE = re.compile(
    r"/(?:social|campus|intern|index)/?[^\"']*?[?&]PageIndex=(\d+)",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*条记录")
_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_FOREIGN_LOCATION_MARKERS = (
    "国外",
    "海外",
    "埃及",
    "印度尼西亚",
    "越南",
    "尼日利亚",
    "菲律宾",
    "老挝",
    "孟加拉",
    "缅甸",
    "美国",
    "加拿大",
    "新加坡",
    "马来西亚",
    "泰国",
    "日本",
    "韩国",
    "澳大利亚",
    "欧洲",
)


@ScraperRegistry.register(ATSType.BEISEN_LEGACY)
class BeisenLegacyScraper(BaseScraper):
    """Scrape one legacy Beisen tenant by zhiye.com subdomain."""

    ats = ATSType.BEISEN_LEGACY
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        categories: tuple[str, ...] = CATEGORIES,
        detail_concurrency: int = DETAIL_CONCURRENCY,
        max_pages: int = MAX_PAGES,
        company_name: str | None = None,
        language: str = "zh",
        country_iso: str | None = "CN",
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.slug = require_host_label(company_slug, provider="BeisenLegacyScraper")
        normalized_categories = tuple(category.strip().title() for category in categories)
        if not normalized_categories or any(
            category not in CATEGORIES for category in normalized_categories
        ):
            raise ScraperError(
                "Beisen legacy categories must be a non-empty subset of "
                f"{CATEGORIES!r}"
            )
        if detail_concurrency < 1:
            raise ScraperError("Beisen legacy detail_concurrency must be positive")
        if max_pages < 1:
            raise ScraperError("Beisen legacy max_pages must be positive")
        self.categories = tuple(dict.fromkeys(normalized_categories))
        self.detail_concurrency = detail_concurrency
        self.max_pages = max_pages
        self.company_name = company_name.strip() if company_name else self.slug
        self.language = language
        self.country_iso = country_iso.upper() if country_iso else None
        self.base_url = f"https://{self.slug}.zhiye.com"

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            rows: list[_ListingRow] = []
            seen: set[str] = set()
            for category in self.categories:
                for row in await self._fetch_category(fetch, category):
                    if row.ats_id in seen:
                        continue
                    seen.add(row.ats_id)
                    rows.append(row)
            if not rows and self.categories == CATEGORIES:
                rows.extend(await self._fetch_index(fetch))
            if self.include_descriptions and rows:
                await self._enrich_rows(fetch, rows)
        return [self._build_job(row) for row in rows]

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            parsed_url = urlsplit(str(job.url))
            path = parsed_url.path
            if parsed_url.query:
                path = f"{path}?{parsed_url.query}"
            async with self.make_fetcher() as fetch:
                page = await self._get_html(fetch, path)
            if page is None:
                return None
            row = _ListingRow(
                ats_id=job.ats_id or "",
                title=job.title,
                category="Social",
            )
            _parse_detail(page, row)
            return row.description

        return self._run_sync(run())

    async def _fetch_category(
        self,
        fetch: Fetcher,
        category: str,
    ) -> list[_ListingRow]:
        rows: list[_ListingRow] = []
        last_page: int | None = None
        for page in range(1, self.max_pages + 1):
            html = await self._get_html(
                fetch,
                LISTING_PATH.format(category=category, page=page),
            )
            if html is None:
                return rows
            page_rows = _parse_listing(html, category=category)
            if not page_rows:
                return rows
            rows.extend(page_rows)
            if last_page is None:
                last_page = _extract_last_page(html, page_size=len(page_rows))
            if last_page is not None and page >= last_page:
                return rows
        raise ScraperError(
            "Beisen legacy pagination reached the safety cap "
            f"for {self.slug!r} {category} ({self.max_pages} pages)"
        )

    async def _fetch_index(self, fetch: Fetcher) -> list[_ListingRow]:
        rows: list[_ListingRow] = []
        last_page: int | None = None
        for page in range(1, self.max_pages + 1):
            html = await self._get_html(fetch, INDEX_PATH.format(page=page))
            if html is None:
                return rows
            page_rows = _parse_index_listing(
                html,
                include_descriptions=self.include_descriptions,
            )
            if not page_rows:
                return rows
            rows.extend(page_rows)
            if last_page is None:
                last_page = _extract_last_page(html, page_size=len(page_rows))
            if last_page is not None and page >= last_page:
                return rows
        raise ScraperError(
            "Beisen legacy pagination reached the safety cap "
            f"for {self.slug!r} Index ({self.max_pages} pages)"
        )

    async def _get_html(self, fetch: Fetcher, path: str) -> str | None:
        response = await fetch.request(
            "GET",
            f"{self.base_url}{path}",
            handled={404},
        )
        if response.status_code == 404:
            return None
        return response.text

    async def _enrich_rows(
        self,
        fetch: Fetcher,
        rows: list[_ListingRow],
    ) -> None:
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def enrich(row: _ListingRow) -> None:
            if row.description:
                return
            async with semaphore:
                try:
                    html = await self._get_html(fetch, row.detail_path)
                except ScraperError:
                    return
                if html is not None:
                    _parse_detail(html, row)

        await asyncio.gather(*(enrich(row) for row in rows))

    def _build_job(self, row: _ListingRow) -> Job:
        country_iso = self.country_iso
        if row.location and any(
            marker in row.location for marker in _FOREIGN_LOCATION_MARKERS
        ):
            country_iso = None
        raw: dict[str, object] = {
            "legacy_portal": True,
            "tenant": self.slug,
            "category": row.category,
        }
        for key, value in (
            ("brand", row.brand),
            ("recruit_region", row.recruit_region),
            ("headcount", row.headcount),
            ("kind", row.kind),
            ("job_category", row.job_category),
            ("end_date", row.end_date),
        ):
            if value not in (None, ""):
                raw[key] = value
        return Job(
            url=HttpUrl(f"{self.base_url}{row.detail_path}"),
            title=row.title,
            company=self.company_name,
            ats_type=ATSType.BEISEN_LEGACY,
            ats_id=row.ats_id,
            location=row.location,
            country_iso=country_iso,
            region="Asia" if country_iso in {"CN", "HK", "MO", "TW"} else None,
            description=row.description,
            salary_summary=row.salary_summary,
            employment_type=_employment_type(row.kind),
            department=row.department or _category_label(row.category),
            posted_at=row.posted_at,
            fetched_at=datetime.now(UTC),
            language=self.language,
            commitment=row.kind,
            raw=raw,
        )


@dataclass(slots=True)
class _ListingRow:
    ats_id: str
    title: str
    category: str
    location: str | None = None
    posted_at: datetime | None = None
    headcount: int | None = None
    brand: str | None = None
    recruit_region: str | None = None
    salary_summary: str | None = None
    kind: str | None = None
    description: str | None = None
    end_date: str | None = None
    department: str | None = None
    job_category: str | None = None
    detail_path: str = ""

    def __post_init__(self) -> None:
        if not self.detail_path:
            self.detail_path = DETAIL_PATH.format(ats_id=self.ats_id)


def _soup(html: str):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - installation failure
        raise ScraperError(
            "Beisen legacy scraper requires beautifulsoup4 "
            "(install ats-scrapers[scrapers])"
        ) from exc
    return BeautifulSoup(html, "html.parser")


def _parse_listing(html: str, *, category: str) -> list[_ListingRow]:
    soup = _soup(html)
    table = soup.select_one("table.jobsTable, table.listtable")
    if table is None:
        return []
    rows: list[_ListingRow] = []
    for table_row in table.find_all("tr"):
        link = table_row.find("a", href=_JOB_ID_RE)
        if link is None:
            continue
        match = _JOB_ID_RE.search(str(link.get("href") or ""))
        title = str(link.get("title") or link.get_text(" ", strip=True)).strip()
        if match is None or not title:
            continue
        row = _ListingRow(
            ats_id=match.group(1),
            title=title,
            category=category,
        )
        _parse_listing_columns(table_row.find_all("td")[1:], row)
        rows.append(row)
    return rows


def _parse_index_listing(
    html: str,
    *,
    include_descriptions: bool,
) -> list[_ListingRow]:
    soup = _soup(html)
    rows: list[_ListingRow] = []
    for card in soup.select("div.zwlb > ul > li"):
        apply_link = card.find(attrs={"jobadid": True})
        heading = card.find("h2")
        if apply_link is None or heading is None:
            continue
        ats_id = str(apply_link.get("jobadid") or "").strip()
        spans = heading.find_all("span", recursive=False)
        title = spans[0].get_text(" ", strip=True) if spans else ""
        if not ats_id.isdigit() or not title:
            continue
        row = _ListingRow(
            ats_id=ats_id,
            title=title,
            category="Index",
            detail_path=f"/zwxq?jobId={ats_id}",
        )
        heading_values = [span.get_text(" ", strip=True) for span in spans]
        if len(heading_values) > 1:
            row.department = heading_values[1] or None
        if len(heading_values) > 2:
            row.job_category = heading_values[2] or None
        if len(heading_values) > 3:
            row.location = heading_values[3] or None
        if len(heading_values) > 4:
            row.posted_at = _parse_date(heading_values[4])
        _parse_detail(
            str(card),
            row,
            include_description=include_descriptions,
        )
        rows.append(row)
    return rows


def _parse_listing_columns(cells, row: _ListingRow) -> None:
    for cell in cells:
        value = str(cell.get("title") or cell.get_text(" ", strip=True)).strip()
        if not value or value in {"-", "&nbsp;"}:
            continue
        if _DATE_RE.fullmatch(value) and row.posted_at is None:
            row.posted_at = _parse_date(value)
            continue
        if value.isdigit() and row.headcount is None:
            row.headcount = int(value)
            continue
        if _looks_like_location(value):
            if row.location is None or len(value) > len(row.location):
                row.location = value
            continue
        if row.recruit_region is None and len(value) <= 12:
            row.recruit_region = value
        elif row.brand is None:
            row.brand = value


def _extract_last_page(html: str, *, page_size: int) -> int | None:
    soup = _soup(html)
    for link in soup.find_all("a"):
        if link.get_text(" ", strip=True) != "尾页":
            continue
        match = _PAGE_RE.search(str(link.get("href") or ""))
        if match is not None:
            return int(match.group(1))
    total_match = _TOTAL_RE.search(soup.get_text(" ", strip=True))
    if total_match is None or page_size < 1:
        return None
    total = int(total_match.group(1))
    return (total + page_size - 1) // page_size if total else 0


def _parse_detail(
    html: str,
    row: _ListingRow,
    *,
    include_description: bool = True,
) -> None:
    soup = _soup(html)
    for label in soup.select("li.ntitle"):
        label_class = next(
            (
                class_name.removeprefix("td-").casefold()
                for class_name in label.get("class", [])
                if class_name.startswith("td-")
            ),
            "",
        )
        value_node = label.find_next_sibling("li")
        if value_node is None:
            continue
        value = str(
            value_node.get("title") or value_node.get_text(" ", strip=True)
        ).strip()
        if not value or value in {"-", "&nbsp;"}:
            continue
        if "salar" in label_class:
            row.salary_summary = row.salary_summary or value
        elif "kind" in label_class:
            row.kind = row.kind or value
        elif "postdate" in label_class and row.posted_at is None:
            row.posted_at = _parse_date(value)
        elif "endtime" in label_class:
            row.end_date = row.end_date or value
        elif "headcount" in label_class and row.headcount is None:
            with contextlib.suppress(ValueError):
                row.headcount = int(value)
        elif "cities" in label_class and row.location is None:
            row.location = value
    if row.location is None:
        city = soup.select_one("li.nvcity")
        if city is not None:
            row.location = city.get_text(" ", strip=True) or None
    for item in soup.select("div.zwlbbt span, div.zwlbbt2 span"):
        label, separator, value = item.get_text(" ", strip=True).partition("：")
        if not separator or not value:
            continue
        value = value.strip()
        if label == "工作地点":
            row.location = row.location or value
        elif label == "需求部门":
            row.department = row.department or value
        elif label == "岗位类别":
            row.job_category = row.job_category or value
        elif label == "工作性质":
            row.kind = row.kind or value
        elif label == "薪资待遇":
            row.salary_summary = row.salary_summary or value
        elif label == "招聘人数" and row.headcount is None:
            with contextlib.suppress(ValueError):
                row.headcount = int(value)
    if not include_description:
        return
    body = soup.select_one("div.xiangqingtext, div.zwlbbm")
    if body is not None:
        text = body.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        row.description = text or None


def _parse_date(value: str) -> datetime | None:
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None
    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], date_format).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _looks_like_location(value: str) -> bool:
    if any(character in value for character in "省市区县州国"):
        return True
    return "-" in value and any(character in value for character in "国外全国")


def _employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    if "实习" in value:
        return "INTERN"
    if "兼职" in value:
        return "PART_TIME"
    if "临时" in value or "短期" in value:
        return "TEMPORARY"
    if any(label in value for label in ("合同", "外包", "派遣")):
        return "CONTRACT"
    if "全职" in value or "正式" in value:
        return "FULL_TIME"
    return None


def _category_label(category: str) -> str | None:
    return {
        "Social": "社会招聘",
        "Campus": "校园招聘",
        "Intern": "实习生招聘",
    }.get(category)
