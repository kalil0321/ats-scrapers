from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import WinTalentScraper
from ats_scrapers.scrapers.base import ScraperRegistry

HOST = "dfmc.hotjob.cn"
SUITE = "SU61d501d92f9d24431f65f608"
PORTAL = f"https://{HOST}/{SUITE}"
CONFIG_URL = f"https://{HOST}/wecruit/suite/config/{SUITE}"
LIST_URL = f"https://{HOST}/wecruit/positionInfo/listPosition/{SUITE}"
DETAIL_URL = (
    f"https://{HOST}/wecruit/positionInfo/listPositionDetail/{SUITE}"
)
LEGACY_PORTAL = "https://www.hotjob.cn/wt/caict"
LEGACY_LIST_URL = (
    f"{LEGACY_PORTAL}/web/index/"
    "webPosition210!getPostListByConditionShowPic"
)


@pytest.fixture(autouse=True)
def stable_cache_buster(monkeypatch) -> None:
    # Only the scraper's own cache-buster: ``time.time_ns`` itself stays as
    # it is for everything else in the process.
    monkeypatch.setattr(
        "ats_scrapers.scrapers.wintalent._new_cache_bust",
        lambda: "123",
    )


def _payload(data: dict[str, object], *, state: object = "200") -> dict:
    return {"state": state, "type": "success", "data": data}


def _config(*recruit_types: str, names: dict[str, str] | None = None) -> dict:
    return _payload(
        {
            "companyName": "东风汽车集团有限公司",
            "suiteName": "东风汽车招聘微官网",
            "webSiteLanguage": ["zh-CN"],
            "recruitTypeNameMap": {
                f"{recruit_type}_cn": (names or {}).get(
                    recruit_type, f"type {recruit_type}"
                )
                for recruit_type in recruit_types
            },
        }
    )


def _item(
    post_id: str,
    *,
    title: str = "后端工程师",
    location: str = "武汉市",
    company: str = "东风汽车集团股份有限公司",
) -> dict[str, object]:
    return {
        "postId": post_id,
        "postName": title,
        "company": company,
        "workPlaceStr": location,
        "workTypeStr": "全职",
        "postCode": "DFMC020918",
        "externalKey": "493801",
        "postTypeName": "技术类",
        "orgCode": "0/30",
        "projectId": 0,
        "projectName": "日常招聘",
        "educationStr": "本科及以上",
        "publishFirstDate": "2026-07-15 10:59:55",
        "publishDate": "2026-07-16 11:00:00",
    }


def _page(
    items: list[dict[str, object]],
    *,
    page: int = 1,
    total: int | None = None,
    total_pages: int = 1,
    page_size: int = 50,
) -> dict:
    return _payload(
        {
            "pageForm": {
                "currentPage": page,
                "pageSize": page_size,
                "pageData": items,
                "dataCount": len(items) if total is None else total,
                "totalPage": total_pages,
            },
            "positonNum": len(items) if total is None else total,
        }
    )


def _list_url(
    recruit_type: str,
    page: int = 1,
    page_size: int = 50,
) -> str:
    return (
        f"{LIST_URL}?recruitType={recruit_type}&currentPage={page}"
        f"&pageSize={page_size}&_=123-{recruit_type}-{page}"
    )


def _legacy_html(
    rows: list[tuple[str, str, str, str, str]],
    *,
    total: int | None = None,
    company: str = "中国信通院招聘官网",
) -> str:
    rendered = "".join(
        f"""
        <tr>
          <td><a title="{title}" href="/wt/caict/web/index/webPosition210!getOnePosition?postIdEnc={post_id}&amp;recruitType=&amp;brandCode=1">{title}</a></td>
          <td>{department}</td>
          <td>1</td>
          <td>{location}</td>
          <td>{posted}</td>
        </tr>
        """
        for post_id, title, department, location, posted in rows
    )
    count = len(rows) if total is None else total
    return f"""
      <html>
        <head><title>{company}</title></head>
        <body>
          <table>
            <tr>
              <th>职位名称</th><th>所属机构</th><th>招聘人数</th>
              <th>工作地点</th><th>发布时间</th>
            </tr>
            {rendered}
          </table>
          <div>当前页面: 1/1 共 {count} 条记录</div>
        </body>
      </html>
    """


def test_registry_resolves_wintalent() -> None:
    assert ScraperRegistry.get(ATSType.WINTALENT) is WinTalentScraper


def test_fetches_modern_jobs_with_structured_fields(httpx_mock) -> None:
    post_id = "67b2a2c11eb80555b7a39fb9"
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item(post_id)]),
    )

    job = WinTalentScraper(PORTAL, include_descriptions=False).fetch()[0]

    assert job.ats_type is ATSType.WINTALENT
    assert job.ats_id == post_id
    assert job.title == "后端工程师"
    assert job.company == "东风汽车集团股份有限公司"
    assert str(job.url) == (
        f"{PORTAL}/mc/detail?postId={post_id}&recruitType=2"
    )
    assert job.location == "武汉市"
    assert job.country_iso == "CN"
    assert job.region == "Asia"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "全职"
    assert job.requisition_id == "DFMC020918"
    assert job.posted_at == datetime(
        2026, 7, 15, 2, 59, 55, tzinfo=UTC
    )
    assert job.fetched_at is not None
    # The portal gives no locale for a listing, and the script of its title
    # does not say what language the listing is in.
    assert job.language is None
    assert job.raw["suite"] == SUITE
    assert job.raw["external_key"] == "493801"
    assert job.raw["education"] == "本科及以上"


def test_fetches_all_recruit_types_and_deduplicates_ids(httpx_mock) -> None:
    shared = _item("67b2a2c11eb80555b7a39fb9")
    second = _item("6a3cd0971d4c30777af06d79")
    httpx_mock.add_response(url=CONFIG_URL, json=_config("1", "12"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("1"),
        json=_page([shared]),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("12"),
        json=_page([shared, second]),
    )

    jobs = WinTalentScraper(PORTAL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == [
        "67b2a2c11eb80555b7a39fb9",
        "6a3cd0971d4c30777af06d79",
    ]
    assert jobs[1].employment_type == "FULL_TIME"


def test_skips_configured_empty_recruit_type(httpx_mock) -> None:
    job = _item("67b2a2c11eb80555b7a39fb9")
    httpx_mock.add_response(url=CONFIG_URL, json=_config("1", "2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("1"),
        json=_page(
            [],
            page=0,
            total=0,
            total_pages=0,
            page_size=10,
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([job]),
    )

    jobs = WinTalentScraper(PORTAL, include_descriptions=False).fetch()

    assert [item.ats_id for item in jobs] == [
        "67b2a2c11eb80555b7a39fb9"
    ]


@pytest.mark.parametrize(
    ("board_name", "expected"),
    [
        ("实习生招聘", "INTERN"),
        ("Interns", "INTERN"),
        # The same number is another board on other portals.
        ("博士招聘", None),
        ("飞行招聘", None),
    ],
)
def test_job_without_work_type_follows_the_name_of_its_board(
    httpx_mock, board_name, expected
) -> None:
    item = _item("67b2a2c11eb80555b7a39fb9")
    item.pop("workTypeStr")
    httpx_mock.add_response(
        url=CONFIG_URL, json=_config("12", names={"12": board_name})
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("12"),
        json=_page([item]),
    )

    job = WinTalentScraper(PORTAL, include_descriptions=False).fetch()[0]

    assert job.employment_type == expected


@pytest.mark.parametrize(
    ("work_type", "title", "category", "expected"),
    [
        # The portal's own work type decides, whatever the title says.
        ("全职", "International Sales Manager", "销售类", "FULL_TIME"),
        ("全职", "合同管理专员", "合同管理类", "FULL_TIME"),
        ("Full time", "Legal Contract Manager", "法务类", "FULL_TIME"),
        ("兼职", "内容运营实习生", "运营类", "PART_TIME"),
        ("实习", "后端工程师", "技术类", "INTERN"),
        ("合同工", "后端工程师", "技术类", "CONTRACT"),
        # A value that names two types gives none.
        ("全职/实习", "后端工程师", "技术类", None),
        # No work type: the title and the job category are not read.
        (None, "内容运营实习生", "实习生", None),
        (None, "Internal Auditor", "审计类", None),
        (None, "合同管理专员", "合同管理类", None),
    ],
)
def test_employment_type_comes_from_the_work_type_field_only(
    httpx_mock, work_type, title, category, expected
) -> None:
    item = _item("67b2a2c11eb80555b7a39fb9", title=title)
    item["postTypeName"] = category
    if work_type is None:
        item.pop("workTypeStr")
    else:
        item["workTypeStr"] = work_type
    httpx_mock.add_response(url=CONFIG_URL, json=_config("1"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("1"),
        json=_page([item]),
    )

    job = WinTalentScraper(PORTAL, include_descriptions=False).fetch()[0]

    assert job.employment_type == expected
    assert job.commitment == work_type


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("深圳市-南山区", ("CN", "Asia")),
        ("北京市、上海市", ("CN", "Asia")),
        ("上海", ("CN", "Asia")),
        ("广东省", ("CN", "Asia")),
        ("香港", ("HK", "Asia")),
        ("台北市", ("TW", "Asia")),
        # A place that is not a name of the region picker gives no country,
        # whatever suffix it ends with.
        ("日本大阪市", (None, None)),
        ("美国加州湾区", (None, None)),
        ("印度尼西亚", (None, None)),
        ("全部地区", (None, None)),
        ("其他国家/地区", (None, None)),
        # Places in two countries, or one place that is not known.
        ("香港、深圳", (None, None)),
        ("香港、深圳市", (None, None)),
        ("香港、新加坡", (None, None)),
        ("长春市、墨西哥", (None, None)),
    ],
)
def test_country_is_read_from_the_region_picker_names_only(
    httpx_mock, place, expected
) -> None:
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item("67b2a2c11eb80555b7a39fb9", location=place)]),
    )

    job = WinTalentScraper(PORTAL, include_descriptions=False).fetch()[0]

    assert (job.country_iso, job.region) == expected


def test_fetches_modern_description(httpx_mock) -> None:
    post_id = "67b2a2c11eb80555b7a39fb9"
    httpx_mock.add_response(url=CONFIG_URL, json=_config("12"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("12"),
        json=_page([_item(post_id)]),
    )
    scraper = WinTalentScraper(PORTAL, include_descriptions=False)
    job = scraper.fetch()[0]
    httpx_mock.add_response(
        method="POST",
        url=(
            f"{DETAIL_URL}?postId={post_id}&recruitType=12&_=123"
        ),
        json=_payload(
            {
                "workContent": "<p>根据岗位安排</p>",
                "serviceCondition": "1、认真负责；\n2、沟通良好。",
            }
        ),
    )

    assert scraper.get_description(job) == (
        "工作内容\n根据岗位安排\n\n"
        "任职要求\n1、认真负责；\n2、沟通良好。"
    )


def test_default_fetch_hydrates_descriptions(httpx_mock) -> None:
    post_id = "67b2a2c11eb80555b7a39fb9"
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item(post_id)]),
    )
    httpx_mock.add_response(
        method="POST",
        url=(
            f"{DETAIL_URL}?postId={post_id}&recruitType=2&_=123"
        ),
        json=_payload(
            {
                "workContent": "构建可靠系统",
                "serviceCondition": "熟悉 Python",
            }
        ),
    )

    job = WinTalentScraper(PORTAL).fetch()[0]

    assert job.description == (
        "工作内容\n构建可靠系统\n\n任职要求\n熟悉 Python"
    )


def test_modern_pagination_is_complete(httpx_mock) -> None:
    first = _item("67b2a2c11eb80555b7a39fb9")
    second = _item("6a3cd0971d4c30777af06d79")
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 1),
        json=_page(
            [first],
            page=1,
            total=2,
            total_pages=2,
            page_size=1,
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 2, page_size=1),
        json=_page(
            [second],
            page=2,
            total=2,
            total_pages=2,
            page_size=1,
        ),
    )

    jobs = WinTalentScraper(PORTAL, include_descriptions=False).fetch()

    assert len(jobs) == 2


def test_jobs_already_listed_under_an_earlier_recruit_type_do_not_stop_paging(
    httpx_mock,
) -> None:
    first = _item("67b2a2c11eb80555b7a39fb9")
    second = _item("6a3cd0971d4c30777af06d79")
    third = _item("6a3cd0971d4c30777af06d80")
    httpx_mock.add_response(url=CONFIG_URL, json=_config("1", "2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("1"),
        json=_page([first, second]),
    )
    # The first page of type 2 holds only jobs of type 1: a valid page.
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 1),
        json=_page(
            [first, second], page=1, total=3, total_pages=2, page_size=2
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 2, page_size=2),
        json=_page([third], page=2, total=3, total_pages=2, page_size=2),
    )

    jobs = WinTalentScraper(PORTAL, include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == [
        "67b2a2c11eb80555b7a39fb9",
        "6a3cd0971d4c30777af06d79",
        "6a3cd0971d4c30777af06d80",
    ]


@pytest.mark.parametrize(
    ("second_page_ids", "error"),
    [
        # The same page twice: nothing new.
        (
            ["67b2a2c11eb80555b7a39fb9", "6a3cd0971d4c30777af06d79"],
            "repeated page 2 without new job IDs",
        ),
        # Four rows received, three distinct jobs for four advertised.
        (
            ["6a3cd0971d4c30777af06d79", "6a3cd0971d4c30777af06d80"],
            "expected 4 jobs, received 3 distinct ones",
        ),
    ],
)
def test_modern_repeated_rows_fail_closed(
    httpx_mock, second_page_ids, error
) -> None:
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 1),
        json=_page(
            [
                _item("67b2a2c11eb80555b7a39fb9"),
                _item("6a3cd0971d4c30777af06d79"),
            ],
            page=1,
            total=4,
            total_pages=2,
            page_size=2,
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 2, page_size=2),
        json=_page(
            [_item(post_id) for post_id in second_page_ids],
            page=2,
            total=4,
            total_pages=2,
            page_size=2,
        ),
    )

    with pytest.raises(ScraperError, match=error):
        WinTalentScraper(PORTAL, include_descriptions=False).fetch()


def test_unreadable_description_keeps_the_job_and_logs_why(
    httpx_mock, caplog
) -> None:
    post_id = "67b2a2c11eb80555b7a39fb9"
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item(post_id)]),
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{DETAIL_URL}?postId={post_id}&recruitType=2&_=123",
        json={"state": "500", "type": "error", "msg": "职位不存在"},
    )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.wintalent"):
        job = WinTalentScraper(PORTAL).fetch()[0]

    assert job.description is None
    assert f"postId={post_id}" in caplog.text
    assert "state='500'" in caplog.text


def test_posting_closed_on_its_own_page_is_dropped(httpx_mock) -> None:
    open_id = "67b2a2c11eb80555b7a39fb9"
    closed_id = "6a3cd0971d4c30777af06d79"
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item(open_id), _item(closed_id)]),
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{DETAIL_URL}?postId={open_id}&recruitType=2&_=123",
        json=_payload({"workContent": "负责后端开发", "serviceCondition": ""}),
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{DETAIL_URL}?postId={closed_id}&recruitType=2&_=123",
        json={
            "msg": "该职位招聘已经关闭，请关注其他职位，谢谢!",
            "state": "1017",
            "type": "warning",
        },
    )

    scraper = WinTalentScraper(PORTAL)
    jobs = scraper.fetch()

    assert [job.ats_id for job in jobs] == [open_id]

    # Asked for the description of the closed posting alone: nothing.
    listed = WinTalentScraper(PORTAL, include_descriptions=False)
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page([_item(closed_id)]),
    )
    closed_job = listed.fetch()[0]
    httpx_mock.add_response(
        method="POST",
        url=f"{DETAIL_URL}?postId={closed_id}&recruitType=2&_=123",
        json={"msg": "closed", "state": "1017", "type": "warning"},
    )
    assert listed.get_description(closed_job) is None


def test_description_lookup_on_a_job_without_raw_data_returns_nothing() -> None:
    from ats_scrapers.models import Job

    job = Job(
        url=f"{PORTAL}/mc/detail?postId=67b2a2c11eb80555b7a39fb9&recruitType=2",
        title="后端工程师",
        company="东风汽车集团有限公司",
        ats_type=ATSType.WINTALENT,
        ats_id="67b2a2c11eb80555b7a39fb9",
        raw=None,
    )

    assert WinTalentScraper(PORTAL).get_description(job) is None


def test_modern_total_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2"),
        json=_page(
            [_item("67b2a2c11eb80555b7a39fb9")],
            total=2,
        ),
    )

    with pytest.raises(ScraperError, match="expected 2 jobs"):
        WinTalentScraper(PORTAL, include_descriptions=False).fetch()


def test_modern_metadata_change_fails_closed(httpx_mock) -> None:
    first = _item("67b2a2c11eb80555b7a39fb9")
    second = _item("6a3cd0971d4c30777af06d79")
    httpx_mock.add_response(url=CONFIG_URL, json=_config("2"))
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 1),
        json=_page(
            [first],
            page=1,
            total=2,
            total_pages=2,
            page_size=1,
        ),
    )
    httpx_mock.add_response(
        method="POST",
        url=_list_url("2", 2, page_size=1),
        json=_page(
            [second],
            page=2,
            total=3,
            total_pages=2,
            page_size=1,
        ),
    )

    with pytest.raises(ScraperError, match="metadata changed"):
        WinTalentScraper(PORTAL, include_descriptions=False).fetch()


def test_non_success_state_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=CONFIG_URL,
        json={"state": "500", "type": "error", "msg": "官网不存在"},
    )

    with pytest.raises(ScraperError, match="state='500'"):
        WinTalentScraper(PORTAL).fetch()


def test_fetches_legacy_server_rendered_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url=(
            f"{LEGACY_LIST_URL}?pc.currentPage=1&pc.rowSize=1000"
        ),
        text=_legacy_html(
            [
                (
                    "c3e9b1bc3e10c241",
                    "综合管理专员",
                    "无线电研究中心",
                    "乌鲁木齐市",
                    "2026-07-29",
                ),
                (
                    "100da3e3a26ab97f",
                    "数字化转型研究员26SBZ37",
                    "技术与标准研究所",
                    "北京市",
                    "2026-07-27",
                ),
            ]
        ),
    )

    jobs = WinTalentScraper(
        LEGACY_PORTAL,
        include_descriptions=False,
    ).fetch()

    assert len(jobs) == 2
    assert jobs[0].ats_id == "legacy:/wt/caict:c3e9b1bc3e10c241"
    assert jobs[0].title == "综合管理专员"
    assert jobs[0].company == "中国信通院"
    assert jobs[0].department == "无线电研究中心"
    assert jobs[0].location == "乌鲁木齐市"
    assert jobs[0].country_iso == "CN"
    assert jobs[0].region == "Asia"
    assert jobs[0].posted_at == datetime(
        2026, 7, 28, 16, 0, tzinfo=UTC
    )
    assert jobs[0].raw["head_count"] == "1"
    assert "postIdEnc=c3e9b1bc3e10c241" in str(jobs[0].url)


def test_legacy_total_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=(
            f"{LEGACY_LIST_URL}?pc.currentPage=1&pc.rowSize=1000"
        ),
        text=_legacy_html(
            [
                (
                    "c3e9b1bc3e10c241",
                    "综合管理专员",
                    "无线电研究中心",
                    "北京市",
                    "2026-07-29",
                )
            ],
            total=2,
        ),
    )

    with pytest.raises(ScraperError, match="expected 2 jobs"):
        WinTalentScraper(LEGACY_PORTAL).fetch()


def test_legacy_repeated_page_fails_closed(httpx_mock) -> None:
    row = (
        "c3e9b1bc3e10c241",
        "综合管理专员",
        "无线电研究中心",
        "北京市",
        "2026-07-29",
    )
    for page in (1, 2):
        httpx_mock.add_response(
            url=f"{LEGACY_LIST_URL}?pc.currentPage={page}&pc.rowSize=1000",
            text=_legacy_html([row], total=1001),
        )

    with pytest.raises(
        ScraperError, match="repeated page 2 without new job IDs"
    ):
        WinTalentScraper(LEGACY_PORTAL).fetch()


def test_legacy_listing_with_jobs_and_a_total_of_zero_fails_closed(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=f"{LEGACY_LIST_URL}?pc.currentPage=1&pc.rowSize=1000",
        text=_legacy_html(
            [
                (
                    "c3e9b1bc3e10c241",
                    "综合管理专员",
                    "无线电研究中心",
                    "北京市",
                    "2026-07-29",
                )
            ],
            total=0,
        ),
    )

    with pytest.raises(ScraperError, match="1 jobs with a total of zero"):
        WinTalentScraper(LEGACY_PORTAL).fetch()


def test_legacy_listing_without_jobs_and_a_total_of_zero_is_empty(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=f"{LEGACY_LIST_URL}?pc.currentPage=1&pc.rowSize=1000",
        text=_legacy_html([], total=0),
    )

    assert WinTalentScraper(LEGACY_PORTAL).fetch() == []


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "dfmc.hotjob.cn/SU61d501d92f9d24431f65f608",
        "http://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608",
        "https://evil.example/SU61d501d92f9d24431f65f608",
        "https://evil.hotjob.cn.attacker.io/SU61d501d92f9d24431f65f608",
        "https://dfmc.hotjob.cn/SU-not-valid",
        "https://www.hotjob.cn/wt/../caict",
        f"{PORTAL}?redirect=https://evil.example",
        f"https://user:pass@{HOST}/{SUITE}",
    ],
)
def test_rejects_untrusted_portal_urls(slug: str) -> None:
    with pytest.raises(ScraperError, match="WinTalent slug"):
        WinTalentScraper(slug)


def test_preserves_case_sensitive_suite_identifier() -> None:
    scraper = WinTalentScraper(PORTAL)

    assert scraper.suite == SUITE
    assert scraper.portal_url == PORTAL
