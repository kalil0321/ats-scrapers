"""WinTalent / Hotjob multi-tenant ATS scraper.

WinTalent, operated by Dayee, serves public employer career sites on
``*.hotjob.cn``. Current sites use an anonymous JSON API under
``/wecruit``; older sites expose server-rendered listings below
``/wt/{tenant}``.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, quote, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

MODERN_PAGE_SIZE = 50
DESCRIPTION_CONCURRENCY = 4
LEGACY_PAGE_SIZE = 1_000
MAX_PAGES = 10_000

_HOTJOB_HOST_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*hotjob\.cn",
    re.IGNORECASE,
)
_SUITE_RE = re.compile(r"SU[a-f0-9]{24}", re.IGNORECASE)
_LEGACY_TENANT_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?",
    re.IGNORECASE,
)
_POST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_LEGACY_TOTAL_RE = re.compile(r"共\s*(\d[\d,]*)\s*条(?:记录)?")
_RECRUIT_SUFFIX_RE = re.compile(
    r"(?:校园招聘|社会招聘|人才招聘|招聘官网|招聘网站|招聘中心|招聘)$"
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")

_EMPLOYMENT_TYPES: tuple[tuple[str, EmploymentType], ...] = (
    ("实习", "INTERN"),
    ("intern", "INTERN"),
    ("兼职", "PART_TIME"),
    ("part time", "PART_TIME"),
    ("part-time", "PART_TIME"),
    ("合同", "CONTRACT"),
    ("contract", "CONTRACT"),
    ("临时", "TEMPORARY"),
    ("temporary", "TEMPORARY"),
    ("全职", "FULL_TIME"),
    ("full time", "FULL_TIME"),
    ("full-time", "FULL_TIME"),
)


@ScraperRegistry.register(ATSType.WINTALENT)
class WinTalentScraper(BaseScraper):
    """Scrape one current or legacy WinTalent employer portal."""

    ats = ATSType.WINTALENT
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        if "://" not in company_slug:
            raise ScraperError(
                "WinTalent slug must be a credential-free HTTPS "
                "hotjob.cn employer URL"
            )
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        parsed = urlparse(
            company_slug
            if "://" in company_slug
            else f"https://{company_slug}"
        )
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not _HOTJOB_HOST_RE.fullmatch(host)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ScraperError(
                "WinTalent slug must be a credential-free HTTPS "
                "hotjob.cn employer URL"
            )
        segments = [
            segment for segment in parsed.path.split("/") if segment
        ]
        if len(segments) == 1 and _SUITE_RE.fullmatch(segments[0]):
            self.variant = "modern"
            self.suite = segments[0]
            self.legacy_tenant = None
            path = f"/{self.suite}"
        elif (
            len(segments) == 2
            and segments[0].casefold() == "wt"
            and _LEGACY_TENANT_RE.fullmatch(segments[1])
        ):
            self.variant = "legacy"
            self.suite = None
            self.legacy_tenant = segments[1]
            path = f"/wt/{segments[1]}"
        else:
            raise ScraperError(
                "WinTalent slug must end in /SU<24 hex chars> or "
                "/wt/<legacy tenant>"
            )
        self.host = host
        self.base_url = f"https://{host}"
        self.portal_url = f"{self.base_url}{path}"
        self.company_slug = self.portal_url
        self.company_name = _clean_text(company_name)
        self._resolved_company: str | None = None
        self._cache_bust = str(time.time_ns())

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            if self.variant == "modern":
                return await self._fetch_modern(fetch)
            return await self._fetch_legacy(fetch)

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        if job.raw.get("variant") != "modern":
            return None

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                return await self._fetch_modern_description(fetch, job)

        return self._run_sync(run())

    async def _fetch_modern(self, fetch: Fetcher) -> list[Job]:
        assert self.suite is not None
        config = await fetch.get_json(
            f"{self.base_url}/wecruit/suite/config/{self.suite}"
        )
        data = _modern_data(
            config,
            provider=f"WinTalent ({self.portal_url}) config",
        )
        self._resolved_company = (
            self.company_name
            or _clean_text(data.get("companyName"))
            or _clean_text(data.get("suiteName"))
            or self.host.split(".", 1)[0]
        )
        recruit_map = data.get("recruitTypeNameMap")
        if not isinstance(recruit_map, dict):
            raise ScraperError(
                f"WinTalent ({self.portal_url}) config is missing "
                "recruitTypeNameMap"
            )
        recruit_types = sorted(
            {
                key.split("_", 1)[0]
                for raw_key in recruit_map
                if (key := str(raw_key)).split("_", 1)[0].isdigit()
            },
            key=int,
        )
        if not recruit_types:
            raise ScraperError(
                f"WinTalent ({self.portal_url}) config has no recruit types"
            )

        jobs: list[Job] = []
        seen: set[str] = set()
        for recruit_type in recruit_types:
            await self._fetch_modern_recruit_type(
                fetch,
                recruit_type,
                jobs,
                seen,
            )
        if self.include_descriptions:
            await self._hydrate_modern_descriptions(fetch, jobs)
        return jobs

    async def _fetch_modern_recruit_type(
        self,
        fetch: Fetcher,
        recruit_type: str,
        jobs: list[Job],
        seen: set[str],
    ) -> None:
        assert self.suite is not None
        endpoint = (
            f"{self.base_url}/wecruit/positionInfo/"
            f"listPosition/{self.suite}"
        )
        expected_total: int | None = None
        total_pages: int | None = None
        effective_page_size: int | None = None
        raw_rows = 0
        for page in range(1, MAX_PAGES + 1):
            requested_page_size = (
                effective_page_size or MODERN_PAGE_SIZE
            )
            response = await fetch.request(
                "POST",
                endpoint,
                params={
                    "recruitType": recruit_type,
                    "currentPage": page,
                    "pageSize": requested_page_size,
                    "_": f"{self._cache_bust}-{recruit_type}-{page}",
                },
                headers={
                    "Accept": "application/json",
                    "Referer": (
                        f"{self.portal_url}/mc/position/society"
                    ),
                },
            )
            payload = response.json()
            data = _modern_data(
                payload,
                provider=(
                    f"WinTalent ({self.portal_url}) recruit_type="
                    f"{recruit_type} page={page}"
                ),
            )
            page_form = data.get("pageForm")
            if not isinstance(page_form, dict):
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) page {page} is "
                    "missing pageForm"
                )
            items = page_form.get("pageData")
            if not isinstance(items, list):
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) page {page} is "
                    "missing pageData"
                )
            page_total = _required_nonnegative_int(
                page_form.get("dataCount"),
                field="dataCount",
                portal=self.portal_url,
            )
            page_count = _required_nonnegative_int(
                page_form.get("totalPage"),
                field="totalPage",
                portal=self.portal_url,
            )
            if expected_total is None and page_total == 0:
                if items or page_count != 0:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) returned "
                        "inconsistent empty pagination metadata"
                    )
                return
            reported_page = _coerce_int(page_form.get("currentPage"))
            if reported_page is not None and reported_page != page:
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) requested page {page}, "
                    f"received page {reported_page}"
                )
            reported_page_size = _required_positive_int(
                page_form.get("pageSize"),
                field="pageSize",
                portal=self.portal_url,
            )
            if len(items) > reported_page_size:
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) page {page} returned "
                    f"{len(items)} rows for pageSize={reported_page_size}"
                )
            if expected_total is None:
                expected_total = page_total
                total_pages = page_count
                effective_page_size = reported_page_size
                computed_pages = math.ceil(
                    expected_total / effective_page_size
                )
                if total_pages != computed_pages:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) reports "
                        f"totalPage={total_pages}, expected {computed_pages} "
                        f"for dataCount={expected_total} and "
                        f"pageSize={effective_page_size}"
                    )
                if total_pages == 0:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) reports "
                        f"{expected_total} jobs but zero pages"
                    )
            else:
                if reported_page_size != effective_page_size:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) page size changed "
                        f"from {effective_page_size} to "
                        f"{reported_page_size} while fetching"
                    )
                if page_total != expected_total or page_count != total_pages:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) pagination "
                        "metadata changed while fetching"
                    )
            if not items:
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) pagination stopped "
                    f"at {raw_rows} of {expected_total} rows"
                )

            new_count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_rows += 1
                job = self._parse_modern_job(item, recruit_type)
                if job.ats_id in seen:
                    continue
                seen.add(str(job.ats_id))
                jobs.append(job)
                new_count += 1
            if new_count == 0 and raw_rows < (expected_total or 0):
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) repeated page {page} "
                    "without new job IDs"
                )
            if total_pages is not None and page >= total_pages:
                if raw_rows != expected_total:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) expected "
                        f"{expected_total} jobs, received {raw_rows}"
                    )
                return
        raise ScraperError(
            f"WinTalent ({self.portal_url}) reached the {MAX_PAGES}-page "
            "safety cap"
        )

    def _parse_modern_job(
        self,
        item: dict[str, Any],
        recruit_type: str,
    ) -> Job:
        assert self.suite is not None
        post_id = _required_post_id(
            item.get("postId"),
            portal=self.portal_url,
        )
        title = _required_text(
            item.get("postName"),
            field="postName",
            portal=self.portal_url,
        )
        company = (
            _clean_text(item.get("company"))
            or self._resolved_company
            or self.host.split(".", 1)[0]
        )
        canonical_url = (
            f"{self.portal_url}/mc/detail?"
            f"postId={quote(post_id, safe='')}&"
            f"recruitType={quote(recruit_type, safe='')}"
        )
        employment = _clean_text(item.get("workTypeStr"))
        location = _clean_text(item.get("workPlaceStr"))
        country_iso, region = _location_geography(location)
        raw = {
            "variant": "modern",
            "portal_url": self.portal_url,
            "suite": self.suite,
            "recruit_type": recruit_type,
        }
        for source, destination in (
            ("externalKey", "external_key"),
            ("postCode", "post_code"),
            ("postType", "post_type"),
            ("postTypeName", "post_type_name"),
            ("orgCode", "org_code"),
            ("projectId", "project_id"),
            ("projectName", "project_name"),
            ("educationStr", "education"),
            ("endDate", "end_date"),
            ("publishDate", "updated_at"),
            ("longTermRelease", "long_term_release"),
        ):
            value = item.get(source)
            if value not in (None, "", [], {}):
                raw[destination] = value
        return Job(
            url=canonical_url,
            title=title,
            company=company,
            ats_type=ATSType.WINTALENT,
            ats_id=post_id,
            location=location,
            country_iso=country_iso,
            region=region,
            employment_type=(
                _employment_type(
                    " ".join(
                        filter(
                            None,
                            (
                                employment,
                                _clean_text(item.get("postTypeName")),
                                title,
                            ),
                        )
                    )
                )
                or ("INTERN" if recruit_type == "12" else None)
            ),
            department=_clean_text(item.get("department")),
            requisition_id=(
                _clean_text(item.get("postCode"))
                or _clean_text(item.get("externalKey"))
            ),
            apply_url=canonical_url,
            commitment=employment,
            posted_at=(
                _parse_china_datetime(item.get("publishFirstDate"))
                or _parse_china_datetime(item.get("publishDate"))
            ),
            fetched_at=datetime.now(UTC),
            language=_language_for_text(title),
            raw=raw,
        )

    async def _fetch_modern_description(
        self,
        fetch: Fetcher,
        job: Job,
    ) -> str | None:
        suite = _clean_text(job.raw.get("suite"))
        recruit_type = _clean_text(job.raw.get("recruit_type"))
        if suite is None or recruit_type is None:
            return None
        response = await fetch.request(
            "POST",
            (
                f"{self.base_url}/wecruit/positionInfo/"
                f"listPositionDetail/{suite}"
            ),
            params={
                "postId": str(job.ats_id),
                "recruitType": recruit_type,
                "_": self._cache_bust,
            },
            headers={
                "Accept": "application/json",
                "Referer": str(job.url),
            },
        )
        data = _modern_data(
            response.json(),
            provider=f"WinTalent job {job.ats_id}",
        )
        return _join_sections(
            ("工作内容", data.get("workContent")),
            ("任职要求", data.get("serviceCondition")),
        )

    async def _hydrate_modern_descriptions(
        self,
        fetch: Fetcher,
        jobs: list[Job],
    ) -> None:
        semaphore = asyncio.Semaphore(DESCRIPTION_CONCURRENCY)

        async def hydrate(job: Job) -> None:
            async with semaphore:
                try:
                    description = await self._fetch_modern_description(
                        fetch,
                        job,
                    )
                except ScraperError:
                    return
                if description:
                    job.description = description[:25_000]

        await asyncio.gather(*(hydrate(job) for job in jobs))

    async def _fetch_legacy(self, fetch: Fetcher) -> list[Job]:
        assert self.legacy_tenant is not None
        endpoint = (
            f"{self.portal_url}/web/index/"
            "webPosition210!getPostListByConditionShowPic"
        )
        jobs: list[Job] = []
        seen: set[str] = set()
        expected_total: int | None = None
        total_pages = 1
        for page in range(1, MAX_PAGES + 1):
            html = await fetch.get_text(
                endpoint,
                params={
                    "pc.currentPage": page,
                    "pc.rowSize": LEGACY_PAGE_SIZE,
                },
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": f"{self.portal_url}/web/index",
                },
            )
            page_jobs, page_total, page_company = _parse_legacy_page(
                html,
                endpoint=endpoint,
                portal=self.portal_url,
                fallback_company=(
                    self.company_name
                    or self._resolved_company
                    or ""
                ),
            )
            if self._resolved_company is None:
                self._resolved_company = (
                    self.company_name
                    or page_company
                    or self.legacy_tenant
                )
            if expected_total is None:
                expected_total = page_total
                total_pages = max(
                    1,
                    math.ceil(expected_total / LEGACY_PAGE_SIZE),
                )
                if expected_total == 0:
                    return []
            elif page_total != expected_total:
                raise ScraperError(
                    f"WinTalent legacy ({self.portal_url}) total changed "
                    "while paginating"
                )
            if not page_jobs:
                raise ScraperError(
                    f"WinTalent legacy ({self.portal_url}) pagination "
                    f"stopped at page {page} of {total_pages}"
                )
            new_count = 0
            for job in page_jobs:
                if job.ats_id in seen:
                    continue
                seen.add(str(job.ats_id))
                jobs.append(job)
                new_count += 1
            if new_count == 0 and len(jobs) < expected_total:
                raise ScraperError(
                    f"WinTalent legacy ({self.portal_url}) repeated "
                    f"page {page} without new job IDs"
                )
            if page >= total_pages:
                break
        else:
            raise ScraperError(
                f"WinTalent legacy ({self.portal_url}) reached the "
                f"{MAX_PAGES}-page safety cap"
            )
        if expected_total is not None and len(jobs) != expected_total:
            raise ScraperError(
                f"WinTalent legacy ({self.portal_url}) expected "
                f"{expected_total} jobs, parsed {len(jobs)}"
            )
        return jobs


def _modern_data(payload: object, *, provider: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScraperError(f"{provider} returned a non-object payload")
    if str(payload.get("state")) != "200":
        raise ScraperError(
            f"{provider} returned state={payload.get('state')!r}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ScraperError(f"{provider} returned no data object")
    return data


def _parse_legacy_page(
    html: str,
    *,
    endpoint: str,
    portal: str,
    fallback_company: str,
) -> tuple[list[Job], int, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    total_match = _LEGACY_TOTAL_RE.search(text)
    if total_match is None:
        raise ScraperError(
            f"WinTalent legacy ({portal}) listing has no total count"
        )
    total = int(total_match.group(1).replace(",", ""))
    title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    page_company = _clean_legacy_company(title)
    company = fallback_company or page_company or portal
    jobs: list[Job] = []
    for link in soup.select("a[href*='postIdEnc=']"):
        href = _clean_text(link.get("href"))
        if href is None:
            continue
        parsed_href = urlparse(urljoin(endpoint, href))
        query = parse_qs(parsed_href.query)
        raw_post_id = query.get("postIdEnc", [None])[0]
        if raw_post_id is None:
            continue
        post_id = _required_post_id(raw_post_id, portal=portal)
        title_text = (
            _clean_text(link.get("title"))
            or _clean_text(link.get_text(" ", strip=True))
        )
        if title_text is None:
            raise ScraperError(
                f"WinTalent legacy ({portal}) job {post_id} has no title"
            )
        row = link.find_parent("tr")
        fields = _legacy_row_fields(row)
        canonical_url = parsed_href.geturl()
        location = (
            fields.get("工作地点")
            or fields.get("工作地")
            or fields.get("地点")
        )
        country_iso, region = _location_geography(location)
        jobs.append(
            Job(
                url=canonical_url,
                title=title_text,
                company=company,
                ats_type=ATSType.WINTALENT,
                ats_id=f"legacy:{urlparse(portal).path}:{post_id}",
                location=location,
                country_iso=country_iso,
                region=region,
                department=(
                    fields.get("所属机构")
                    or fields.get("招聘单位")
                    or fields.get("部门")
                ),
                apply_url=canonical_url,
                posted_at=_parse_china_date(
                    fields.get("发布时间")
                    or fields.get("发布日期")
                ),
                fetched_at=datetime.now(UTC),
                language=_language_for_text(title_text),
                raw={
                    "variant": "legacy",
                    "portal_url": portal,
                    "post_id_enc": post_id,
                    "head_count": fields.get("招聘人数"),
                    "fields": fields,
                },
            )
        )
    return jobs, total, page_company


def _legacy_row_fields(row: Any) -> dict[str, str]:
    if row is None:
        return {}
    table = row.find_parent("table")
    headers = [
        _clean_text(header.get_text(" ", strip=True)) or ""
        for header in table.select("tr th")
    ] if table is not None else []
    values = [
        _clean_text(cell.get_text(" ", strip=True)) or ""
        for cell in row.find_all("td", recursive=False)
    ]
    if headers and len(headers) == len(values):
        return {
            header: value
            for header, value in zip(headers, values, strict=True)
            if header and value
        }
    return {}


def _required_post_id(value: object, *, portal: str) -> str:
    post_id = _clean_text(value)
    if post_id is None or not _POST_ID_RE.fullmatch(post_id):
        raise ScraperError(
            f"WinTalent ({portal}) returned malformed post ID {value!r}"
        )
    return post_id


def _required_text(
    value: object,
    *,
    field: str,
    portal: str,
) -> str:
    cleaned = _clean_text(value)
    if cleaned is None:
        raise ScraperError(
            f"WinTalent ({portal}) returned no {field}"
        )
    return cleaned


def _required_nonnegative_int(
    value: object,
    *,
    field: str,
    portal: str,
) -> int:
    parsed = _coerce_int(value)
    if parsed is None or parsed < 0:
        raise ScraperError(
            f"WinTalent ({portal}) returned malformed {field}={value!r}"
        )
    return parsed


def _required_positive_int(
    value: object,
    *,
    field: str,
    portal: str,
) -> int:
    parsed = _coerce_int(value)
    if parsed is None or parsed <= 0:
        raise ScraperError(
            f"WinTalent ({portal}) returned malformed {field}={value!r}"
        )
    return parsed


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(
        " ",
        strip=True,
    )
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized or None


def _clean_multiline(value: object) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(
        "\n",
        strip=True,
    )
    lines = [
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in text.splitlines()
    ]
    normalized = "\n".join(line for line in lines if line)
    return normalized or None


def _join_sections(*sections: tuple[str, object]) -> str | None:
    rendered = [
        f"{heading}\n{body}"
        for heading, value in sections
        if (body := _clean_multiline(value)) is not None
    ]
    return "\n\n".join(rendered) or None


def _employment_type(value: str | None) -> EmploymentType | None:
    normalized = (value or "").casefold()
    return next(
        (
            employment_type
            for marker, employment_type in _EMPLOYMENT_TYPES
            if marker in normalized
        ),
        None,
    )


def _location_geography(
    location: str | None,
) -> tuple[str | None, str | None]:
    if location is None or any(
        marker in location
        for marker in ("全部地区", "其它", "全球", "海外")
    ):
        return None, None

    special_regions = {
        "香港": "HK",
        "澳门": "MO",
        "台湾": "TW",
    }
    matched = {
        code for marker, code in special_regions.items() if marker in location
    }
    parts = [
        part.strip()
        for part in re.split(r"[、,，;/]+", location)
        if part.strip()
    ]
    if matched:
        has_mainland_part = any(
            not any(marker in part for marker in special_regions)
            and (
                re.search(
                    r"(?:省|市|自治区|自治州|地区|盟|县)",
                    part,
                )
                or any(
                    city in part
                    for city in ("北京", "上海", "天津", "重庆")
                )
            )
            for part in parts
        )
        if has_mainland_part:
            return None, None
        if len(matched) == 1:
            return matched.pop(), "Asia"
        return None, None
    mainland = bool(
        re.search(r"(?:省|市|自治区|自治州|地区|盟|县|区)", location)
        or any(city in location for city in ("北京", "上海", "天津", "重庆"))
    )
    if mainland:
        return "CN", "Asia"
    return None, None


def _language_for_text(value: str) -> str | None:
    if re.search(r"[\u3400-\u9fff]", value):
        return "zh"
    if re.search(r"[A-Za-z]", value):
        return "en"
    return None


def _parse_china_datetime(value: object) -> datetime | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, pattern).replace(
                tzinfo=_SHANGHAI
            )
        except ValueError:
            continue
        return parsed.astimezone(UTC)
    return None


def _parse_china_date(value: str | None) -> datetime | None:
    return _parse_china_datetime(value)


def _clean_legacy_company(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    company = _RECRUIT_SUFFIX_RE.sub("", cleaned).strip(" -_|")
    return company or None
