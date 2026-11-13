"""WinTalent / Hotjob multi-tenant ATS scraper.

WinTalent, operated by Dayee, serves public employer career sites on
``*.hotjob.cn``. Current sites use an anonymous JSON API under
``/wecruit``; older sites expose server-rendered listings below
``/wt/{tenant}``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

MODERN_PAGE_SIZE = 50
# The answer of a job page whose posting is closed while the list still
# shows it: "该职位招聘已经关闭，请关注其他职位，谢谢!".
_CLOSED_POSTING_STATE = "1017"
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
# China Standard Time is UTC+8 all year (no daylight saving time), so a
# fixed offset is exact and needs no time zone database on the machine.
_CHINA_TIME = timezone(timedelta(hours=8))

# Labels of the portal's own work-type field. English labels are matched
# as whole words: "International" is not "intern".
_EMPLOYMENT_TYPES: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (re.compile(r"实习|\binterns?(?:hip)?\b"), "INTERN"),
    (re.compile(r"兼职|\bpart[ -]time\b"), "PART_TIME"),
    (re.compile(r"合同|\bcontract(?:or)?\b"), "CONTRACT"),
    (re.compile(r"临时|\btemporary\b"), "TEMPORARY"),
    (re.compile(r"全职|\bfull[ -]time\b"), "FULL_TIME"),
)

# The place of a job comes from the portal's region picker. Its first level
# is a division of mainland China, a division of Taiwan, Hong Kong, Macau,
# a country name, or a value such as "其它" or "全部地区". A country is
# given only for the names below, read from the picker itself (live
# portals, 13 Nov 2026) plus the provinces; any other value gives none.
_MAINLAND_NAMES = (
    "七台河市 万宁市 三亚市 三明市 三沙市 三门峡市 上海市 上饶市 东方市 "
    "东莞市 东营市 中卫市 中山市 临夏回族自治州 临汾市 临沂市 临沧市 临高县 "
    "丹东市 丽水市 丽江市 乌兰察布市 乌海市 乌鲁木齐市 乐东黎族自治县 乐山市 "
    "九江市 云南省 云浮市 五家渠市 五指山市 亳州市 仙桃市 伊春市 "
    "伊犁哈萨克自治州 佛山市 佳木斯市 保亭黎族苗族自治县 保定市 保山市 信阳市 "
    "儋州市 克孜勒苏柯尔克孜自治州 克拉玛依市 六安市 六盘水市 兰州市 兴安盟 "
    "内江市 内蒙古自治区 凉山彝族自治州 包头市 北京市 北屯市 北海市 十堰市 "
    "南京市 南充市 南宁市 南平市 南昌市 南通市 南阳市 博尔塔拉蒙古自治州 "
    "厦门市 双河市 双鸭山市 可克达拉市 台州市 合肥市 吉安市 吉林市 吉林省 "
    "吐鲁番市 吕梁市 吴忠市 周口市 呼伦贝尔市 呼和浩特市 和田地区 咸宁市 "
    "咸阳市 哈密市 哈尔滨市 唐山市 商丘市 商洛市 喀什地区 嘉兴市 嘉峪关市 "
    "四川省 四平市 固原市 图木舒克市 塔城地区 大兴安岭地区 大同市 大庆市 "
    "大理白族自治州 大连市 天水市 天津市 天门市 太原市 威海市 娄底市 孝感市 "
    "宁夏回族自治区 宁德市 宁波市 安庆市 安康市 安徽省 安阳市 安顺市 定安县 "
    "定西市 宜宾市 宜昌市 宜春市 宝鸡市 宣城市 宿州市 宿迁市 屯昌县 山东省 "
    "山南市 山西省 岳阳市 崇左市 巴中市 巴彦淖尔市 巴音郭楞蒙古自治州 常州市 "
    "常德市 平凉市 平顶山市 广东省 广元市 广安市 广州市 广西壮族自治区 庆阳市 "
    "廊坊市 延安市 延边朝鲜族自治州 开封市 张家口市 张家界市 张掖市 徐州市 "
    "德宏傣族景颇族自治州 德州市 德阳市 忻州市 怀化市 怒江傈僳族自治州 "
    "恩施土家族苗族自治州 惠州市 成都市 扬州市 承德市 抚州市 抚顺市 拉萨市 "
    "揭阳市 攀枝花市 文山壮族苗族自治州 文昌市 新乡市 新余市 新疆维吾尔自治区 "
    "无锡市 日喀则市 日照市 昆明市 昆玉市 昌吉回族自治州 昌江黎族自治县 昌都市 "
    "昭通市 晋中市 晋城市 普洱市 景德镇市 曲靖市 朔州市 朝阳市 本溪市 来宾市 "
    "杭州市 松原市 林芝市 果洛藏族自治州 枣庄市 柳州市 株洲市 桂林市 梅州市 "
    "梧州市 楚雄彝族自治州 榆林市 武威市 武汉市 毕节市 永州市 汉中市 汕头市 "
    "汕尾市 江苏省 江西省 江门市 池州市 沈阳市 沧州市 河北省 河南省 河池市 "
    "河源市 泉州市 泰安市 泰州市 泸州市 洛阳市 济南市 济宁市 济源市 浙江省 "
    "海东市 海北藏族自治州 海南省 海南藏族自治州 海口市 海西蒙古族藏族自治州 "
    "淄博市 淮北市 淮南市 淮安市 深圳市 清远市 温州市 渭南市 湖北省 湖南省 "
    "湖州市 湘潭市 湘西土家族苗族自治州 湛江市 滁州市 滨州市 漯河市 漳州市 "
    "潍坊市 潜江市 潮州市 澄迈县 濮阳市 烟台市 焦作市 牡丹江市 特克斯县 "
    "玉林市 玉树藏族自治州 玉溪市 珠海市 琼中黎族苗族自治县 琼海市 "
    "甘南藏族自治州 甘孜藏族自治州 甘肃省 白城市 白山市 白沙黎族自治县 白银市 "
    "百色市 益阳市 盐城市 盘锦市 眉山市 石嘴山市 石家庄市 石河子市 神农架林区 "
    "福州市 福建省 秦皇岛市 红河哈尼族彝族自治州 绍兴市 绥化市 绵阳市 聊城市 "
    "肇庆市 自贡市 舟山市 芜湖市 苏州市 茂名市 荆州市 荆门市 莆田市 莱芜市 "
    "菏泽市 萍乡市 营口市 葫芦岛市 蚌埠市 衡水市 衡阳市 衢州市 襄阳市 "
    "西双版纳傣族自治州 西宁市 西安市 西藏自治区 许昌市 贵州省 贵港市 贵阳市 "
    "贺州市 资阳市 赣州市 赤峰市 辽宁省 辽源市 辽阳市 达州市 运城市 连云港市 "
    "迪庆藏族自治州 通化市 通辽市 遂宁市 遵义市 邢台市 那曲市 邯郸市 邵阳市 "
    "郑州市 郴州市 鄂尔多斯市 鄂州市 酒泉市 重庆市 金华市 金昌市 钦州市 "
    "铁岭市 铁门关市 铜仁市 铜川市 铜陵市 银川市 锡林郭勒盟 锦州市 镇江市 "
    "长春市 长沙市 长治市 阜新市 阜阳市 防城港市 阳江市 阳泉市 阿克苏地区 "
    "阿勒泰地区 阿坝藏族羌族自治州 阿拉善盟 阿拉尔市 阿里地区 陇南市 陕西省 "
    "陵水黎族自治县 随州市 雅安市 青岛市 青海省 鞍山市 韶关市 马鞍山市 "
    "驻马店市 鸡西市 鹤壁市 鹤岗市 鹰潭市 黄冈市 黄南藏族自治州 黄山市 黄石市 "
    "黑河市 黑龙江省 黔东南苗族侗族自治州 黔南布依族苗族自治州 "
    "黔西南布依族苗族自治州 齐齐哈尔市 龙岩市 "
)
_TAIWAN_NAMES = (
    "云林县 南投县 台东县 台中市 台北市 台南市 嘉义县 嘉义市 基隆市 宜兰县 "
    "屏东县 彰化县 新北市 新竹县 新竹市 桃园市 澎湖县 花莲县 苗栗县 连江县 "
    "金门县 高雄市 "
)
_PLACE_COUNTRY: dict[str, str] = {
    **dict.fromkeys(_MAINLAND_NAMES.split(), "CN"),
    **dict.fromkeys(("北京", "上海", "天津", "重庆"), "CN"),
    **dict.fromkeys(_TAIWAN_NAMES.split(), "TW"),
    **dict.fromkeys(("台湾", "台湾省"), "TW"),
    **dict.fromkeys(("香港", "香港特别行政区"), "HK"),
    **dict.fromkeys(("澳门", "澳门特别行政区"), "MO"),
}


class _PostingClosedError(Exception):
    """The job page says the posting is closed."""


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
        # Recruit types the portal itself names as internships.
        self._intern_recruit_types: frozenset[str] = frozenset()
        self._cache_bust = _new_cache_bust()

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            if self.variant == "modern":
                return await self._fetch_modern(fetch)
            return await self._fetch_legacy(fetch)

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        if not job.raw or job.raw.get("variant") != "modern":
            return None

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                try:
                    return await self._fetch_modern_description(fetch, job)
                except _PostingClosedError:
                    return None

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
        # The number of a recruit type says nothing fixed: "12" is
        # "实习生招聘" on most portals and "博士招聘" or "飞行招聘" on
        # others. Its name, as the portal gives it, decides.
        self._intern_recruit_types = frozenset(
            recruit_type
            for recruit_type in recruit_types
            if _employment_type(
                " ".join(
                    name
                    for raw_key, value in recruit_map.items()
                    if str(raw_key).split("_", 1)[0] == recruit_type
                    and (name := _clean_text(value)) is not None
                )
            )
            == "INTERN"
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
        # ``seen`` holds the ids of the whole portal and only removes a job
        # listed under two recruit types. Progress and completeness are
        # judged on the ids of this recruit type alone.
        type_ids: set[str] = set()
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
                    f"at {len(type_ids)} of {expected_total} jobs"
                )

            new_in_type = 0
            for item in items:
                if not isinstance(item, dict):
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) page {page} "
                        "returned a malformed job"
                    )
                job = self._parse_modern_job(item, recruit_type)
                job_id = str(job.ats_id)
                if job_id in type_ids:
                    continue
                type_ids.add(job_id)
                new_in_type += 1
                if job_id in seen:
                    continue
                seen.add(job_id)
                jobs.append(job)
            if new_in_type == 0:
                raise ScraperError(
                    f"WinTalent ({self.portal_url}) repeated page {page} "
                    "without new job IDs"
                )
            if total_pages is not None and page >= total_pages:
                if len(type_ids) != expected_total:
                    raise ScraperError(
                        f"WinTalent ({self.portal_url}) expected "
                        f"{expected_total} jobs, received "
                        f"{len(type_ids)} distinct ones"
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
                _employment_type(employment)
                # Without a work type, a job on the portal's internship
                # board is an internship.
                or (
                    "INTERN"
                    if employment is None
                    and recruit_type in self._intern_recruit_types
                    else None
                )
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
        payload = response.json()
        if (
            isinstance(payload, dict)
            and str(payload.get("state")) == _CLOSED_POSTING_STATE
        ):
            raise _PostingClosedError
        data = _modern_data(
            payload,
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
        closed: set[str] = set()

        async def hydrate(job: Job) -> None:
            async with semaphore:
                try:
                    description = await self._fetch_modern_description(
                        fetch,
                        job,
                    )
                except _PostingClosedError:
                    closed.add(str(job.ats_id))
                    return
                except ScraperError as error:
                    # The job is kept without its description; say why.
                    logger.warning(
                        "WinTalent description not read for %s: %s",
                        job.url,
                        error,
                    )
                    return
                if description:
                    job.description = description[:25_000]

        await asyncio.gather(*(hydrate(job) for job in jobs))
        # A posting its own page calls closed is not a job any more.
        if closed:
            jobs[:] = [job for job in jobs if str(job.ats_id) not in closed]

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
                    if page_jobs:
                        raise ScraperError(
                            f"WinTalent legacy ({self.portal_url}) lists "
                            f"{len(page_jobs)} jobs with a total of zero"
                        )
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


def _new_cache_bust() -> str:
    return str(time.time_ns())


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


def _employment_type(work_type: str | None) -> EmploymentType | None:
    """The employment type named by the portal's own work-type field.

    A value that names two types ("全职/实习") gives none; ``commitment``
    keeps the value as the portal wrote it. The title is not read.
    """
    normalized = (work_type or "").casefold()
    found = {
        employment_type
        for pattern, employment_type in _EMPLOYMENT_TYPES
        if pattern.search(normalized)
    }
    return found.pop() if len(found) == 1 else None


def _location_geography(
    location: str | None,
) -> tuple[str | None, str | None]:
    """The country of a job, when every place it lists is in the same one."""
    if location is None:
        return None, None
    countries: set[str] = set()
    for part in re.split(r"[、,，;/]+", location):
        # "深圳市-南山区": the first level decides.
        name = part.strip().split("-", 1)[0].strip()
        if not name:
            continue
        country = _PLACE_COUNTRY.get(name)
        if country is None:
            return None, None
        countries.add(country)
    if len(countries) != 1:
        return None, None
    return countries.pop(), "Asia"


def _parse_china_datetime(value: object) -> datetime | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, pattern).replace(
                tzinfo=_CHINA_TIME
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
