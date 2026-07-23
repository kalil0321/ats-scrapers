"""Tests for Beisen's legacy server-rendered careers portal."""

from __future__ import annotations

from datetime import UTC

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BeisenLegacyScraper, ScraperRegistry
from ats_scrapers.scrapers.beisen_legacy import _extract_last_page

SOCIAL_1 = "https://amer.zhiye.com/Social/?PageIndex=1"
SOCIAL_2 = "https://amer.zhiye.com/Social/?PageIndex=2"
CAMPUS_1 = "https://amer.zhiye.com/Campus/?PageIndex=1"
INTERN_1 = "https://amer.zhiye.com/Intern/?PageIndex=1"
DETAIL_107 = "https://amer.zhiye.com/zpdetail/190810107"
DETAIL_090 = "https://amer.zhiye.com/zpdetail/190790090"
NEWHOPE_SOCIAL_1 = "https://newhope.zhiye.com/Social/?PageIndex=1"
NEWHOPE_CAMPUS_1 = "https://newhope.zhiye.com/Campus/?PageIndex=1"
NEWHOPE_INTERN_1 = "https://newhope.zhiye.com/Intern/?PageIndex=1"
NEWHOPE_INDEX_1 = "https://newhope.zhiye.com/index/?PageIndex=1"
NEWHOPE_DETAIL = "https://newhope.zhiye.com/zwxq?jobId=151211126"


def listing_page(
    rows: str,
    *,
    total: int = 1,
    category: str = "social",
    last_page: int | None = None,
) -> str:
    pager = ""
    if last_page is not None:
        pager = f'<a href="/{category}/?PageIndex={last_page}">尾页</a>'
    return f"""
    <html><body>
      <div class="joblist">
        <table class="jobsTable">
          <tr class="title"><td>职位名称</td></tr>
          {rows}
        </table>
      </div>
      <div class="counts">共{total}条记录</div>
      <div class="pager2">{pager}</div>
    </body></html>
    """


ROW_107 = """
<tr>
  <td><a title="招投标专员" href="/zpdetail/190810107?PageIndex=1">招投标专员</a></td>
  <td title="东莞">东莞</td>
  <td>1</td>
  <td title="广东省-东莞市">广东省-东莞市</td>
  <td>2026-03-28</td>
</tr>
"""

ROW_090 = """
<tr>
  <td><a href="/zpdetail/190790090">国际采购工程师</a></td>
  <td title="广东省">广东省</td>
  <td>2</td>
  <td>2026-01-13</td>
</tr>
"""

ROW_OVERSEAS = """
<tr>
  <td><a href="/zpdetail/190790091">海外销售经理</a></td>
  <td title="国外">国外</td>
  <td>1</td>
  <td>2026-01-13</td>
</tr>
"""

EMPTY_PAGE = listing_page("", total=0)

DETAIL_PAGE = """
<html><body>
  <ul class="xiangqinglist">
    <li class="ntitle td-HasKind">工作性质：</li>
    <li class="nvalue" title="全职">全职</li>
    <li class="ntitle td-HasSalaries">薪资范围：</li>
    <li class="nvalue" title="6000-8000 元/月">6000-8000 元/月</li>
    <li class="ntitle td-HasHeadCount">招聘人数：</li>
    <li class="nvalue" title="1">1</li>
    <li class="ntitle td-HasPostDate">发布时间：</li>
    <li class="nvalue" title="2026-03-28">2026-03-28</li>
    <li class="ntitle td-HasCities">工作地点：</li>
    <li class="nvcity">广东省-东莞市</li>
  </ul>
  <div class="xiangqingtext">
    <div class="section"><p>负责招投标文件。</p></div>
    <div class="section"><p>本科及以上学历。</p></div>
  </div>
  <div class="xiangqingfooter"></div>
</body></html>
"""

INDEX_CARD = """
<li>
  <h2>
    <span>氯化冶金生产负责人(J17698)</span>
    <span>新希望化工</span>
    <span>生产运营类</span>
    <span>彭州市</span>
    <span>2026-07-07</span>
  </h2>
  <div class="zwlbb">
    <div class="zwlbbt">
      <span>工作地点：<b>彭州市</b></span>
      <span>需求部门：<b>新希望化工</b></span>
      <span>岗位类别：<b>生产运营类</b></span>
      <span>工作性质：<b>全职</b></span>
      <span>招聘人数：<b>2</b></span>
      <span>薪资待遇：<b>20000-30000 元/月</b></span>
    </div>
    <div class="zwlbbm">
      <h3>【岗位职责】</h3><p>负责生产管理。</p>
      <h3>【任职要求】</h3><p>本科及以上学历。</p>
    </div>
    <a jobadid="151211126" href="javascript:void(0)">立即申请</a>
  </div>
</li>
"""


def index_page(rows: str, *, total: int = 1) -> str:
    return f"""
    <html><body>
      <div class="zwlb"><ul>{rows}</ul></div>
      <p>共{total}条记录</p>
    </body></html>
    """


INDEX_DETAIL_PAGE = """
<html><body>
  <div class="zwlbbt2">
    <span>工作地点：<b>彭州市</b></span>
    <span>工作性质：<b>全职</b></span>
  </div>
  <div class="zwlbbm">
    <h3>【岗位职责】</h3><p>负责生产管理。</p>
    <h3>【任职要求】</h3><p>本科及以上学历。</p>
  </div>
</body></html>
"""


def add_empty_other_categories(httpx_mock) -> None:
    httpx_mock.add_response(url=CAMPUS_1, text=EMPTY_PAGE)
    httpx_mock.add_response(url=INTERN_1, text=EMPTY_PAGE)


def add_empty_newhope_categories(httpx_mock) -> None:
    httpx_mock.add_response(url=NEWHOPE_SOCIAL_1, text=EMPTY_PAGE)
    httpx_mock.add_response(url=NEWHOPE_CAMPUS_1, text=EMPTY_PAGE)
    httpx_mock.add_response(url=NEWHOPE_INTERN_1, text=EMPTY_PAGE)


def test_registry_resolves_beisen_legacy() -> None:
    assert ScraperRegistry.get(ATSType.BEISEN_LEGACY) is BeisenLegacyScraper


@pytest.mark.parametrize("slug", ["", "bad slug", "bad_slug", "-leading"])
def test_rejects_invalid_slug(slug: str) -> None:
    with pytest.raises(ScraperError):
        BeisenLegacyScraper(slug)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"categories": ()},
        {"categories": ("Unknown",)},
        {"detail_concurrency": 0},
        {"max_pages": 0},
    ],
)
def test_rejects_invalid_configuration(kwargs: dict) -> None:
    with pytest.raises(ScraperError):
        BeisenLegacyScraper("amer", **kwargs)


def test_parses_listing_rows_without_detail_fanout(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SOCIAL_1,
        text=listing_page(ROW_107 + ROW_090, total=2),
    )
    add_empty_other_categories(httpx_mock)

    jobs = BeisenLegacyScraper(
        "amer",
        include_descriptions=False,
        company_name="Amer Sports China",
    ).fetch()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.ats_type is ATSType.BEISEN_LEGACY
    assert first.global_id == "beisen_legacy:190810107"
    assert first.title == "招投标专员"
    assert first.company == "Amer Sports China"
    assert first.location == "广东省-东莞市"
    assert first.country_iso == "CN"
    assert first.region == "Asia"
    assert first.department == "社会招聘"
    assert first.description is None
    assert first.posted_at is not None and first.posted_at.tzinfo is UTC
    assert first.fetched_at.tzinfo is UTC
    assert str(first.url) == "https://amer.zhiye.com/zpdetail/190810107"
    assert first.raw == {
        "legacy_portal": True,
        "tenant": "amer",
        "category": "Social",
        "recruit_region": "东莞",
        "headcount": 1,
    }


def test_leaves_country_blank_for_overseas_location(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SOCIAL_1,
        text=listing_page(ROW_OVERSEAS, total=1),
    )

    [job] = BeisenLegacyScraper(
        "amer",
        categories=("Social",),
        include_descriptions=False,
    ).fetch()

    assert job.location == "国外"
    assert job.country_iso is None
    assert job.region is None


def test_paginates_to_explicit_last_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SOCIAL_1,
        text=listing_page(ROW_107, total=2, last_page=2),
    )
    httpx_mock.add_response(
        url=SOCIAL_2,
        text=listing_page(ROW_090, total=2, last_page=2),
    )

    jobs = BeisenLegacyScraper(
        "amer",
        categories=("Social",),
        include_descriptions=False,
    ).fetch()

    assert [job.ats_id for job in jobs] == ["190810107", "190790090"]


@pytest.mark.parametrize("category", ["social", "campus", "intern", "index"])
def test_last_page_parser_supports_every_category(category: str) -> None:
    html = listing_page(
        ROW_107,
        total=90,
        category=category,
        last_page=6,
    )
    assert _extract_last_page(html, page_size=15) == 6


def test_falls_back_to_index_card_layout(httpx_mock) -> None:
    add_empty_newhope_categories(httpx_mock)
    httpx_mock.add_response(url=NEWHOPE_INDEX_1, text=index_page(INDEX_CARD))

    [job] = BeisenLegacyScraper(
        "newhope",
        include_descriptions=False,
        company_name="New Hope Group",
    ).fetch()

    assert job.ats_id == "151211126"
    assert job.title == "氯化冶金生产负责人(J17698)"
    assert job.company == "New Hope Group"
    assert str(job.url) == NEWHOPE_DETAIL
    assert job.location == "彭州市"
    assert job.department == "新希望化工"
    assert job.salary_summary == "20000-30000 元/月"
    assert job.employment_type == "FULL_TIME"
    assert job.description is None
    assert job.raw["category"] == "Index"
    assert job.raw["job_category"] == "生产运营类"


def test_index_card_description_avoids_detail_fanout(httpx_mock) -> None:
    add_empty_newhope_categories(httpx_mock)
    httpx_mock.add_response(url=NEWHOPE_INDEX_1, text=index_page(INDEX_CARD))

    [job] = BeisenLegacyScraper("newhope").fetch()

    assert "负责生产管理。" in (job.description or "")


def test_index_get_description_uses_zwxq_route(httpx_mock) -> None:
    add_empty_newhope_categories(httpx_mock)
    httpx_mock.add_response(url=NEWHOPE_INDEX_1, text=index_page(INDEX_CARD))
    scraper = BeisenLegacyScraper("newhope", include_descriptions=False)
    [job] = scraper.fetch()
    httpx_mock.add_response(url=NEWHOPE_DETAIL, text=INDEX_DETAIL_PAGE)

    description = scraper.get_description(job)

    assert description is not None
    assert "负责生产管理。" in description
    assert "本科及以上学历。" in description


def test_total_fallback_uses_observed_page_size() -> None:
    html = listing_page(ROW_107, total=31)
    assert _extract_last_page(html, page_size=15) == 3


def test_deduplicates_across_categories(httpx_mock) -> None:
    duplicate_page = listing_page(ROW_107, total=1)
    httpx_mock.add_response(url=SOCIAL_1, text=duplicate_page)
    httpx_mock.add_response(url=CAMPUS_1, text=duplicate_page)
    httpx_mock.add_response(url=INTERN_1, text=EMPTY_PAGE)

    jobs = BeisenLegacyScraper("amer", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["190810107"]


def test_deduplicates_repeated_category_configuration(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SOCIAL_1,
        text=listing_page(ROW_107, total=1),
    )

    jobs = BeisenLegacyScraper(
        "amer",
        categories=("Social", "Social"),
        include_descriptions=False,
    ).fetch()

    assert [job.ats_id for job in jobs] == ["190810107"]


def test_hard_404_is_an_empty_category(httpx_mock) -> None:
    httpx_mock.add_response(url=SOCIAL_1, status_code=404)
    assert BeisenLegacyScraper(
        "amer",
        categories=("Social",),
        include_descriptions=False,
    ).fetch() == []


def test_missing_listing_table_is_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=SOCIAL_1, text="<html>soft 404</html>")
    assert BeisenLegacyScraper(
        "amer",
        categories=("Social",),
        include_descriptions=False,
    ).fetch() == []


def test_raises_instead_of_silently_truncating_at_page_cap(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SOCIAL_1,
        text=listing_page(ROW_107, total=2, last_page=2),
    )
    with pytest.raises(ScraperError, match="reached the safety cap"):
        BeisenLegacyScraper(
            "amer",
            categories=("Social",),
            include_descriptions=False,
            max_pages=1,
        ).fetch()


def test_detail_enrichment_preserves_nested_div_content(httpx_mock) -> None:
    httpx_mock.add_response(url=SOCIAL_1, text=listing_page(ROW_107, total=1))
    httpx_mock.add_response(url=DETAIL_107, text=DETAIL_PAGE)

    [job] = BeisenLegacyScraper("amer", categories=("Social",)).fetch()

    assert job.salary_summary == "6000-8000 元/月"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "全职"
    assert job.description == "负责招投标文件。\n本科及以上学历。"


def test_detail_failure_keeps_listing_job(httpx_mock) -> None:
    httpx_mock.add_response(url=SOCIAL_1, text=listing_page(ROW_107, total=1))
    httpx_mock.add_response(url=DETAIL_107, status_code=500, is_reusable=True)

    [job] = BeisenLegacyScraper("amer", categories=("Social",)).fetch()

    assert job.ats_id == "190810107"
    assert job.description is None


def test_get_description_fetches_one_detail(httpx_mock) -> None:
    scraper = BeisenLegacyScraper(
        "amer", categories=("Social",), include_descriptions=False
    )
    httpx_mock.add_response(url=SOCIAL_1, text=listing_page(ROW_107, total=1))
    [job] = scraper.fetch()
    httpx_mock.add_response(url=DETAIL_107, text=DETAIL_PAGE)

    assert scraper.get_description(job) == "负责招投标文件。\n本科及以上学历。"


def test_empty_date_and_unknown_employment_type_are_safe(httpx_mock) -> None:
    detail = DETAIL_PAGE.replace("全职", "灵活用工").replace("2026-03-28", "")
    httpx_mock.add_response(url=SOCIAL_1, text=listing_page(ROW_107, total=1))
    httpx_mock.add_response(url=DETAIL_107, text=detail)

    [job] = BeisenLegacyScraper("amer", categories=("Social",)).fetch()

    assert job.employment_type is None
    assert job.posted_at is not None
