"""Tests for the JD.com (京东) careers scraper.

JD's public ``/web/job/job_list`` endpoint returns a flat array of job
objects with no wrapper. We don't hit the live API — fixtures below
mirror the real response shape verbatim (subset of fields).
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JDScraper, ScraperRegistry

_LIST_RE = re.compile(r"^https://zhaopin\.jd\.com/web/job/job_list")
_COUNT_RE = re.compile(r"^https://zhaopin\.jd\.com/web/job/job_count")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jd as jd

    monkeypatch.setattr(jd, "MAX_RETRIES", 1)
    monkeypatch.setattr(jd, "RETRY_BASE_DELAY", 0.0)


_SAMPLE_POSTS = [
    {
        "id": 162678,
        "positionId": 217330,
        "positionCode": "00673821",
        "positionName": "商业分析岗",
        "positionNameOpen": "商业分析岗",
        "positionDeptName": "京东零售",
        "jobType": "运营类",
        "jobTypeCode": "YUNGYUN",
        "workCity": "北京市",
        "workCityCode": "11",
        "publishTime": 1778428800000,
        "formatPublishTime": "2026-05-11",
        "isHot": 1,
        "reqNumber": "ZP2605110173",
        "requirementId": 217404,
        "qualification": "1. 教育背景：本科及以上学历。",
        "workContent": "1. 负责运动户外品类的市场分析。",
    },
    {
        "id": 162677,
        "positionId": 217354,
        "positionCode": "00792377",
        "positionName": "解决方案岗",
        # ``positionNameOpen`` deliberately blank — exercise the
        # fallback to ``positionName``.
        "positionNameOpen": "",
        "positionDeptName": "京东工业",
        "jobType": "运营类",
        "jobTypeCode": "YUNGYUN",
        "workCity": "北京市",
        "workCityCode": "11",
        "publishTime": 1778428800000,
        "formatPublishTime": "2026-05-11",
        "isHot": 1,
        "reqNumber": "ZP2605114177",
        "requirementId": 217428,
        "qualification": "本科及以上学历。",
        "workContent": "1. 负责面向重点客户提供行业解决方案。",
    },
]


def test_registry_resolves_jd() -> None:
    assert ScraperRegistry.get(ATSType.JD) is JDScraper


def test_parses_jd_job_payload(httpx_mock) -> None:
    httpx_mock.add_response(url=_LIST_RE, json=_SAMPLE_POSTS)
    # ``job_count`` is consulted only when page 1 returns a full page;
    # 2 < page_size=100 so we don't expect a hit. Optional in case
    # implementation changes.
    httpx_mock.add_response(url=_COUNT_RE, text="2", is_optional=True)

    jobs = JDScraper("any").fetch()

    assert len(jobs) == 2
    first, second = jobs
    assert first.ats_type is ATSType.JD
    assert first.ats_id == "217330"
    assert first.global_id == "jd:217330"
    assert first.title == "商业分析岗"
    assert first.company == "JD.com"
    assert first.location == "北京市"
    assert first.country_iso == "CN"
    assert first.language == "zh"
    assert first.department == "运营类"
    assert first.team == "京东零售"
    assert first.requisition_id == "ZP2605110173"
    assert str(first.url) == "https://zhaopin.jd.com/web/job/job_detail?jobId=217330"
    # Description merges workContent + qualification, in that order.
    assert first.description is not None
    assert first.description.startswith("1. 负责运动户外品类的市场分析。")
    assert "1. 教育背景" in first.description
    # publishTime 1778428800000 ms -> 2026-05-11 (local TZ — assert UTC date)
    assert first.posted_at is not None
    assert first.posted_at.year == 2026 and first.posted_at.month == 5
    assert isinstance(first.fetched_at, datetime)
    assert first.raw == {
        "position_code": "00673821",
        "work_city_code": "11",
        "job_type_code": "YUNGYUN",
        "requirement_id": 217404,
        "is_hot": 1,
    }

    # Second row exercises ``positionNameOpen`` → ``positionName`` fallback
    # and a different subsidiary in ``team``.
    assert second.title == "解决方案岗"
    assert second.team == "京东工业"


def test_skips_post_with_missing_id_and_title(httpx_mock) -> None:
    """Defensive: malformed posts shouldn't crash the whole scrape."""
    httpx_mock.add_response(
        url=_LIST_RE,
        json=[
            {"positionId": None, "positionName": "Ghost"},
            {"positionId": 1, "positionName": ""},
            {"positionId": 2, "positionName": "Real"},
        ],
    )
    httpx_mock.add_response(url=_COUNT_RE, text="3", is_optional=True)

    jobs = JDScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["2"]


def test_html_stripped_from_description(httpx_mock) -> None:
    """Some JD postings embed ``<br/>`` from the WYSIWYG editor."""
    httpx_mock.add_response(
        url=_LIST_RE,
        json=[
            {
                "positionId": 99,
                "positionName": "Test",
                "workContent": "Line 1<br/>Line 2",
                "qualification": "<p>Req</p>",
                "publishTime": 1778428800000,
                "workCity": "上海",
            }
        ],
    )
    httpx_mock.add_response(url=_COUNT_RE, text="1", is_optional=True)

    jobs = JDScraper("any").fetch()
    assert len(jobs) == 1
    desc = jobs[0].description
    assert desc is not None
    assert "<" not in desc and ">" not in desc
    assert "Line 1Line 2" in desc
    assert "Req" in desc


def test_non_200_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LIST_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JDScraper("any").fetch()


def test_short_first_page_skips_pagination(httpx_mock) -> None:
    """When page 1 returns fewer than ``page_size``, no further pages
    are fetched and ``job_count`` is never consulted."""
    httpx_mock.add_response(
        url=_LIST_RE,
        json=[
            {"positionId": 1, "positionName": "Only", "publishTime": 1778428800000}
        ],
    )
    jobs = JDScraper("any", page_size=100).fetch()
    assert len(jobs) == 1
