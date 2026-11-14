from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import JDScraper
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.source_kind import is_headline, source_kind

COUNT = re.compile(r"^https://zhaopin\.jd\.com/web/job/job_count\?")
LIST = re.compile(r"^https://zhaopin\.jd\.com/web/job/job_list\?")
BOARD = "https://zhaopin.jd.com/web/job/job_info_list/3"


def _row(requirement_id: int, **overrides: object) -> dict[str, object]:
    """One opening as the list gives it, staff fields included."""
    row: dict[str, object] = {
        "id": 180000 + requirement_id,
        "requirementId": requirement_id,
        "positionId": 220000 + requirement_id,
        "positionCode": "00682256",
        "positionName": "销售岗",
        "positionNameOpen": " 销售 ",
        "positionDeptName": "京东物流",
        "jobType": "运营类",
        "jobTypeCode": "YUNGYUN",
        "workCity": "江西省",
        "workCityCode": "36",
        "reqNumber": "JDZP00000000266888",
        "publishTime": 1794499200000,
        "formatPublishTime": "2026-11-13",
        "workContent": "1. 负责区域物流客户拓展；\r\n2. 制定并执行<b>客户经营计划</b>。",
        "qualification": "1. 具备物流企业区域客户开拓经验；\n\n\n\n2. 熟悉销售过程管理。",
        "applicantErp": "zhangsan12",
        "recLeader": "李四",
        "reportLeader": "王五",
        "reqName": "赵六",
    }
    row.update(overrides)
    return row


def _serve(httpx_mock, rows: list[dict[str, object]], *, total: int | None = None) -> None:
    """Answer the count, then the list page by page from ``rows``."""
    httpx_mock.add_response(
        method="POST",
        url=COUNT,
        text=str(len(rows) if total is None else total),
        is_reusable=True,
    )

    def page(request):
        import httpx

        index = int(request.url.params["pageIndex"])
        size = int(request.url.params["pageSize"])
        return httpx.Response(200, json=rows[(index - 1) * size : index * size])

    httpx_mock.add_callback(page, method="POST", url=LIST, is_reusable=True)


def _asked(httpx_mock, url: re.Pattern[str]) -> list[dict[str, str]]:
    return [dict(r.url.params) for r in httpx_mock.get_requests() if url.match(str(r.url))]


def test_registry_and_kind() -> None:
    assert ScraperRegistry.get(ATSType.JD) is JDScraper
    assert source_kind("jd") == "company_site"
    assert is_headline("jd")


def test_reads_one_opening(httpx_mock) -> None:
    _serve(httpx_mock, [_row(225563)])

    (job,) = JDScraper("jd").fetch()

    assert _asked(httpx_mock, COUNT) == [
        {"workCityJson": "[]", "jobTypeJson": "[]", "jobSearch": "", "depTypeJson": "[]"}
    ] * 2
    assert _asked(httpx_mock, LIST) == [
        {
            "workCityJson": "[]",
            "jobTypeJson": "[]",
            "jobSearch": "",
            "depTypeJson": "[]",
            "pageIndex": "1",
            "pageSize": "100",
        }
    ]
    assert all(r.method == "POST" and not r.content for r in httpx_mock.get_requests())
    assert job.global_id == "jd:225563"
    assert job.ats_id == "225563"
    assert str(job.url) == f"{BOARD}#requirement-225563"
    assert job.title == "销售"
    assert job.company == "JD.com"
    assert job.department == "京东物流"
    assert job.team == "运营类"
    assert job.location == "江西省"
    assert job.country_iso == "CN"
    assert job.requisition_id == "JDZP00000000266888"
    assert job.posted_at == datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    assert job.language == "zh"
    assert job.description == (
        "岗位描述：\n1. 负责区域物流客户拓展；\n2. 制定并执行 客户经营计划 。\n\n"
        "任职要求：\n1. 具备物流企业区域客户开拓经验；\n\n2. 熟悉销售过程管理。"
    )
    assert job.raw == {
        "listing_id": 405563,
        "position_id": 445563,
        "position_code": "00682256",
        "job_type_code": "YUNGYUN",
        "work_city_code": "36",
    }


def test_nothing_about_a_person_is_stored(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])

    stored = JDScraper("jd").fetch()[0].model_dump_json()

    for personal in ("zhangsan12", "李四", "王五", "赵六"):
        assert personal not in stored


def test_title_falls_back_to_the_internal_name(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1, positionNameOpen="  "), _row(2, positionNameOpen=None)])

    assert [job.title for job in JDScraper("jd").fetch()] == ["销售岗", "销售岗"]


def test_reads_every_page(httpx_mock) -> None:
    _serve(httpx_mock, [_row(i) for i in range(1, 251)])

    jobs = JDScraper("jd").fetch()

    assert len(jobs) == 250
    assert sorted(p["pageIndex"] for p in _asked(httpx_mock, LIST)) == ["1", "2", "3"]
    assert len(_asked(httpx_mock, COUNT)) == 2


def test_a_requisition_on_two_rows_is_one_job(httpx_mock) -> None:
    """The site's list repeats a requisition on neighbouring rows. One
    reading is enough: nothing moved."""
    rows = [_row(i) for i in range(1, 100)] + [_row(99)]
    _serve(httpx_mock, rows)

    jobs = JDScraper("jd").fetch()

    assert len(jobs) == 99
    assert len(_asked(httpx_mock, COUNT)) == 2  # before and after the pages
    assert len(_asked(httpx_mock, LIST)) == 1


def _changing_site(httpx_mock, totals: list[int], readings: list[list[dict[str, object]]]):
    """Totals answered one after the other, and one list per reading."""
    import httpx

    answers = iter(totals)
    lists = iter(readings)
    httpx_mock.add_callback(
        lambda request: httpx.Response(200, text=str(next(answers))),
        method="POST",
        url=COUNT,
        is_reusable=True,
    )
    httpx_mock.add_callback(
        lambda request: httpx.Response(200, json=next(lists)),
        method="POST",
        url=LIST,
        is_reusable=True,
    )


def test_an_opening_closed_during_the_reading_is_not_returned(httpx_mock) -> None:
    """Opening 1 closes while the first reading is under way and opening 3
    was pushed off it. The first reading is dropped whole: mixing the two
    would return the closed opening and call the list complete."""
    _changing_site(
        httpx_mock,
        totals=[3, 2, 2, 2],
        readings=[[_row(1), _row(1), _row(2)], [_row(2), _row(3)]],
    )

    jobs = JDScraper("jd").fetch()

    assert sorted(job.ats_id for job in jobs) == ["2", "3"]
    assert len(_asked(httpx_mock, COUNT)) == 4
    assert len(_asked(httpx_mock, LIST)) == 2


def test_rows_that_do_not_add_up_to_the_total_are_read_again(httpx_mock) -> None:
    _changing_site(
        httpx_mock,
        totals=[3, 3, 3],
        readings=[[_row(1), _row(2)], [_row(1), _row(2), _row(3)]],
    )

    jobs = JDScraper("jd").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2", "3"]
    # A short reading is dropped without asking the total again.
    assert len(_asked(httpx_mock, COUNT)) == 3


def test_a_list_that_keeps_changing_is_refused(httpx_mock) -> None:
    _changing_site(
        httpx_mock,
        totals=[2, 3, 3, 4, 4, 5],
        readings=[[_row(1), _row(2)], [_row(1), _row(2), _row(3)], [_row(i) for i in range(1, 5)]],
    )

    with pytest.raises(ScraperError, match="did not agree with its total in 3 readings"):
        JDScraper("jd").fetch()


def test_a_list_made_mostly_of_repeated_rows_is_refused(httpx_mock) -> None:
    """Rows that add up to the total are not enough: the same page served
    for every page index would add up too."""
    _serve(httpx_mock, [_row(i % 50 + 1) for i in range(100)])

    with pytest.raises(ScraperError, match="announces 100 openings and listed 50 distinct"):
        JDScraper("jd").fetch()


def test_an_empty_site_gives_no_job(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=COUNT, text="0", is_reusable=True)

    assert JDScraper("jd").fetch() == []
    assert _asked(httpx_mock, LIST) == []


@pytest.mark.parametrize("answer", ["", "<html>login</html>", "-1", "12.5", "null"])
def test_a_total_that_is_not_a_number_is_refused(httpx_mock, answer: str) -> None:
    httpx_mock.add_response(method="POST", url=COUNT, text=answer, is_reusable=True)

    with pytest.raises(ScraperError, match="gave no total"):
        JDScraper("jd").fetch()


def test_an_unbelievable_total_is_refused(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=COUNT, text="20001", is_reusable=True)

    with pytest.raises(ScraperError, match="announces 20001 openings"):
        JDScraper("jd").fetch()
    assert _asked(httpx_mock, LIST) == []


@pytest.mark.parametrize(
    "body",
    [{"code": 500}, [1, 2], "login", None],
    ids=["object", "list-of-numbers", "text", "null"],
)
def test_a_page_that_is_not_a_list_of_openings_is_refused(httpx_mock, body: object) -> None:
    httpx_mock.add_response(method="POST", url=COUNT, text="1", is_reusable=True)
    httpx_mock.add_response(method="POST", url=LIST, json=body, is_reusable=True)

    with pytest.raises(ScraperError):
        JDScraper("jd").fetch()


@pytest.mark.parametrize("bad", [None, "", "225563", 0, -4, True, 1.5])
def test_an_opening_without_its_requirement_id_is_refused(httpx_mock, bad: object) -> None:
    _serve(httpx_mock, [_row(1, requirementId=bad)])

    with pytest.raises(ScraperError, match="without its requirementId"):
        JDScraper("jd").fetch()


def test_an_opening_without_a_title_is_refused(httpx_mock) -> None:
    _serve(httpx_mock, [_row(7, positionNameOpen=None, positionName=" ")])

    with pytest.raises(ScraperError, match="opening 7 has no title"):
        JDScraper("jd").fetch()


@pytest.mark.parametrize(
    ("code", "country"),
    [("11", "CN"), ("65", "CN"), ("50", "CN"), ("71", None), ("99", None), ("", None), (None, None), (36, None)],
)
def test_country_is_set_only_for_a_mainland_province_code(
    httpx_mock, code: object, country: str | None
) -> None:
    _serve(httpx_mock, [_row(1, workCityCode=code)])

    assert JDScraper("jd").fetch()[0].country_iso == country


def test_missing_optional_fields(httpx_mock) -> None:
    _serve(
        httpx_mock,
        [
            _row(
                1,
                workContent=None,
                qualification="  ",
                workCity=None,
                positionDeptName="",
                jobType=None,
                reqNumber=None,
                publishTime=None,
                positionCode="",
                jobTypeCode=None,
                workCityCode=None,
            )
        ],
    )

    (job,) = JDScraper("jd").fetch()

    assert job.description is None
    assert job.location is None
    assert job.department is None
    assert job.team is None
    assert job.requisition_id is None
    assert job.posted_at is None
    assert job.country_iso is None
    assert job.raw == {"listing_id": 180001, "position_id": 220001}


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    _serve(httpx_mock, [_row(1)])

    assert JDScraper("jd", include_descriptions=False).fetch()[0].description is None


def test_pipeline_config_fails_closed() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_pipeline

    config = run_pipeline.CONFIGS["jd"]
    assert config["scraper"] is JDScraper
    assert config["singleton"] is True
    assert config["output"] == "jd/jobs.csv"
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
