"""Tests for the current-generation Beisen (zhiye.com) scraper."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

import ats_scrapers.scrapers.beisen as beisen_module
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BeisenScraper, ScraperRegistry

REGISTER_URL = "https://fixturecorp.zhiye.com/portal/registerSystemInfo"
SEARCH_URL = "https://fixturecorp.zhiye.com/api/Jobad/GetJobAdPageList"


def register_html(*, site_name: str = "测试公司招聘门户") -> str:
    config = {
        "Key": "test-key",
        "PortalId": "portal-123",
        "Nested": {"value": {"deeper": True}},
        "BeiAnInfo": {"SiteName": site_name},
    }
    return (
        '<img src="https://portal-oss.zhiye.com/10000/image/default.png">'
        '<img src="//stcms.beisen.com/image/98765/logo.png">'
        f"<script>var BSGlobal = {json.dumps(config)}; var after = true;</script>"
    )


def listing(job_id: int = 123, title: str = "平台工程师") -> dict:
    return {
        "JobAdId": job_id,
        "JobAdName": title,
        "Duty": "<p>构建可靠平台。</p>",
        "Require": "<div>三年以上经验。<br>熟悉 Python。</div>",
        "Salary": "20-30K",
        "LocNames": ["上海", "北京"],
        "Category": "社会招聘",
        "ChangeDate": "2026-07-22T09:45:48",
        "PostDate": "0001-01-01T00:00:00",
        "Id": "internal-123",
        "Org": "研发中心",
        "HeadCount": 2,
    }


def payload(items: list[dict], *, total: int | str | None = None) -> dict:
    return {
        "Code": 200,
        "Count": len(items) if total is None else total,
        "Data": items,
    }


def add_register(httpx_mock, *, site_name: str = "测试公司招聘门户") -> None:
    httpx_mock.add_response(url=REGISTER_URL, text=register_html(site_name=site_name))


def test_registry_resolves_beisen() -> None:
    assert ScraperRegistry.get(ATSType.BEISEN) is BeisenScraper


@pytest.mark.parametrize(
    "slug",
    ["", "bad slug", "bad_slug", "-leading", "trailing-", f"{'a' * 64}"],
)
def test_rejects_invalid_slugs(slug: str) -> None:
    with pytest.raises(ScraperError, match="DNS-safe"):
        BeisenScraper(slug)


def test_rejects_invalid_business_type() -> None:
    with pytest.raises(ScraperError, match="business_type"):
        BeisenScraper("fixturecorp", business_type=3)


def test_parses_nested_config_and_current_listing(httpx_mock) -> None:
    add_register(httpx_mock)
    httpx_mock.add_response(url=SEARCH_URL, json=payload([listing()]))

    [result] = BeisenScraper("FixtureCorp").fetch()

    assert result.ats_type is ATSType.BEISEN
    assert result.ats_id == "123"
    assert result.global_id == "beisen:123"
    assert result.title == "平台工程师"
    assert result.company == "测试公司"
    assert result.location == "上海, 北京"
    assert result.country_iso == "CN"
    assert result.region == "Asia"
    assert result.description == "构建可靠平台。\n\n三年以上经验。\n熟悉 Python。"
    assert result.salary_summary == "20-30K"
    assert result.department == "社会招聘"
    assert result.posted_at is not None and result.posted_at.tzinfo is UTC
    assert result.fetched_at.tzinfo is UTC
    assert str(result.url) == "https://fixturecorp.zhiye.com/portal/jobs/123"
    assert result.raw == {
        "tenant": "fixturecorp",
        "portal_id": "portal-123",
        "tenant_id": "98765",
        "key": "test-key",
        "internal_id": "internal-123",
        "org": "研发中心",
        "category": "社会招聘",
        "head_count": 2,
    }


def test_include_descriptions_false_omits_embedded_content(httpx_mock) -> None:
    add_register(httpx_mock)
    httpx_mock.add_response(url=SEARCH_URL, json=payload([listing()]))
    [result] = BeisenScraper("fixturecorp", include_descriptions=False).fetch()
    assert result.description is None


def test_request_body_matches_public_api(httpx_mock) -> None:
    add_register(httpx_mock)
    httpx_mock.add_response(url=SEARCH_URL, json=payload([]))

    BeisenScraper("fixturecorp", business_type=1).fetch()

    requests = httpx_mock.get_requests()
    assert json.loads(requests[1].content) == {
        "PageIndex": 0,
        "PageSize": 1000,
        "KeyWords": "",
        "SpecialType": 0,
        "PortalId": "portal-123",
        "DisplayFields": [
            "Category",
            "Description",
            "Location",
            "Department",
            "Salary",
        ],
        "Category": ["1"],
    }


def test_paginates_and_deduplicates_jobs(httpx_mock) -> None:
    add_register(httpx_mock)
    first = [listing(index, f"职位 {index}") for index in range(1000)]
    second = [listing(999, "职位 999"), listing(1000, "职位 1000")]
    httpx_mock.add_response(url=SEARCH_URL, json=payload(first, total="1001"))
    httpx_mock.add_response(url=SEARCH_URL, json=payload(second, total="1001"))

    jobs = BeisenScraper("fixturecorp").fetch()

    assert len(jobs) == 1001
    assert jobs[-1].ats_id == "1000"
    requests = httpx_mock.get_requests()
    assert json.loads(requests[1].content)["PageIndex"] == 0
    assert json.loads(requests[2].content)["PageIndex"] == 1


def test_raises_instead_of_silently_truncating_at_page_cap(
    httpx_mock, monkeypatch
) -> None:
    monkeypatch.setattr(beisen_module, "MAX_PAGES", 1)
    add_register(httpx_mock)
    items = [listing(index, f"职位 {index}") for index in range(1000)]
    httpx_mock.add_response(url=SEARCH_URL, json=payload(items, total=2000))

    with pytest.raises(ScraperError, match="reached the safety cap"):
        BeisenScraper("fixturecorp").fetch()


def test_missing_bsglobal_raises_clear_error(httpx_mock) -> None:
    httpx_mock.add_response(url=REGISTER_URL, text="<html>legacy portal</html>")
    with pytest.raises(ScraperError, match="BSGlobal"):
        BeisenScraper("fixturecorp").fetch()


def test_missing_portal_id_raises_clear_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=REGISTER_URL,
        text='<script>var BSGlobal = {"BeiAnInfo":{"SiteName":"Acme"}};</script>',
    )
    with pytest.raises(ScraperError, match="PortalId"):
        BeisenScraper("fixturecorp").fetch()


def test_company_not_found_from_register_404(httpx_mock) -> None:
    httpx_mock.add_response(url=REGISTER_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        BeisenScraper("fixturecorp").fetch()


def test_application_failure_raises(httpx_mock) -> None:
    add_register(httpx_mock)
    httpx_mock.add_response(url=SEARCH_URL, json={"Code": 500, "Message": "boom"})
    with pytest.raises(ScraperError, match="API failure"):
        BeisenScraper("fixturecorp").fetch()


def test_invalid_date_and_empty_suffix_name_fall_back_safely(httpx_mock) -> None:
    add_register(httpx_mock, site_name="招聘门户")
    item = listing()
    item["ChangeDate"] = "not-a-date"
    item["PostDate"] = "2026-07-20T01:02:03+08:00"
    httpx_mock.add_response(url=SEARCH_URL, json=payload([item]))

    [result] = BeisenScraper("fixturecorp").fetch()

    assert result.company == "fixturecorp"
    assert result.posted_at is not None
    assert result.posted_at.tzinfo is UTC
    assert result.posted_at.hour == 17
