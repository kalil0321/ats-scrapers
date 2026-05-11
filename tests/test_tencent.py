"""Tests for Tencent Careers."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, TencentScraper

_API_RE = re.compile(r"^https://careers\.tencent\.com/tencentcareer/api/post/Query")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.tencent as t

    monkeypatch.setattr(t, "MAX_RETRIES", 1)
    monkeypatch.setattr(t, "RETRY_BASE_DELAY", 0.0)


def _payload(posts: list[dict], count: int | None = None) -> dict:
    return {"Code": 200, "Data": {"Count": len(posts) if count is None else count, "Posts": posts}}


def test_registry_resolves_tencent() -> None:
    assert ScraperRegistry.get(ATSType.TENCENT) is TencentScraper


def test_parses_tencent_job_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload([
            {
                "PostId": "1965706679647625216",
                "RecruitPostId": 114723,
                "RecruitPostName": "云数据库内核研发工程师",
                "CountryName": "中国",
                "LocationName": "深圳",
                "BGName": "TEG",
                "ProductName": "TDSQL MySQL",
                "CategoryName": "技术",
                "Responsibility": "负责数据库内核模块的架构设计和特性开发",
                "Requirement": "五年以上相关经验",
                "LastUpdateTime": "2026年05月11日",
                "PostURL": "http://careers.tencent.com/jobdesc.html?postId=1965706679647625216",
                "RequireWorkYearsName": "五年以上工作经验",
            }
        ]),
    )

    jobs = TencentScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.TENCENT
    assert j.ats_id == "1965706679647625216"
    assert j.title == "云数据库内核研发工程师"
    assert j.company == "Tencent"
    assert j.location == "深圳, 中国"
    assert j.department == "技术"
    assert j.team == "TDSQL MySQL"
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw["business_group"] == "TEG"


def test_paginates_and_dedupes(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload([
            {"PostId": "1", "RecruitPostName": "One"},
            {"PostId": "2", "RecruitPostName": "Two"},
        ], count=3),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_payload([
            {"PostId": "2", "RecruitPostName": "Two repeated"},
            {"PostId": "3", "RecruitPostName": "Three"},
        ], count=3),
    )

    jobs = TencentScraper("any", page_size=2).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_non_200_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        TencentScraper("any").fetch()
