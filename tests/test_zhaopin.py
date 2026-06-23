"""Tests for the Zhaopin China job board scraper."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, ZhaopinScraper

_API_RE = re.compile(r"^https://fe-api\.zhaopin\.com/c/i/search/positions")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.zhaopin as z

    monkeypatch.setattr(z, "MAX_RETRIES", 1)


def _payload(items: list[dict], *, is_end: int = 1, count: int | None = None) -> dict:
    return {
        "code": 200,
        "apiCode": 200,
        "data": {
            "count": len(items) if count is None else count,
            "isEndPage": is_end,
            "list": items,
        },
    }


def _item(job_id: str = "CC000544460J40700710516") -> dict:
    return {
        "companyName": "软通动力信息技术(集团)股份有限公司",
        "companyNumber": "CZ000544460",
        "companySize": "10000人以上",
        "salary60": "1.1-1.5万",
        "cityId": "779",
        "cityDistrict": "",
        "education": "本科",
        "workingExp": "经验不限",
        "industryName": "软件/IT服务",
        "cardCustomJson": (
            '{"address":"东莞 南城街道","companyName":"软通动力信息技术(集团)",'
            '"salary60":"1.1-1.5万"}'
        ),
        "jobDetailData": {
            "position": {
                "base": {
                    "positionName": "python爬虫工程师",
                    "positionNumber": job_id,
                    "positionUrl": "",
                    "positionWorkingExp": "经验不限",
                    "salary": "1.1-1.5万",
                    "workType": "全职",
                },
                "desc": {
                    "description": "设计和实现高效稳定的爬虫程序。",
                    "labels": ["Python", "爬虫开发"],
                },
                "workLocation": {
                    "address": "工作地点：东莞 · 南城街道",
                    "positionCityId": "779",
                },
            }
        },
    }


def test_registry_resolves_zhaopin() -> None:
    assert ScraperRegistry.get(ATSType.ZHAOPIN) is ZhaopinScraper


def test_parses_zhaopin_payload(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_payload([_item()]))

    jobs = ZhaopinScraper("zhaopin", city_codes=("489",), keywords=("python",)).fetch()

    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.ZHAOPIN
    assert j.ats_id == "CC000544460J40700710516"
    assert j.title == "python爬虫工程师"
    assert j.company == "软通动力信息技术(集团)股份有限公司"
    assert j.location == "工作地点：东莞 · 南城街道"
    assert j.country_iso == "CN"
    assert j.salary_currency == "CNY"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 11000
    assert j.salary_max == 15000
    assert j.experience == 0
    assert j.department == "软件/IT服务"
    assert j.description == "设计和实现高效稳定的爬虫程序。"
    assert j.raw is not None
    assert j.raw["company_number"] == "CZ000544460"


def test_paginates_queries_and_dedupes(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_payload([_item("1")], is_end=0))
    httpx_mock.add_response(url=_API_RE, json=_payload([_item("1"), _item("2")], is_end=1))
    httpx_mock.add_response(url=_API_RE, json=_payload([_item("3")], is_end=1))

    jobs = ZhaopinScraper(
        "zhaopin",
        city_codes=("489",),
        keywords=("python", "java"),
        max_pages=2,
    ).fetch()

    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_empty_page_stops_query(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_payload([]))

    assert ZhaopinScraper("zhaopin", city_codes=("489",), keywords=("nope",)).fetch() == []


def test_application_error_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json={"code": 500, "data": {}})
    with pytest.raises(ScraperError):
        ZhaopinScraper("zhaopin", city_codes=("489",), keywords=("python",)).fetch()
