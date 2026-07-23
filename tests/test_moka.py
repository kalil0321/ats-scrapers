"""Tests for the Moka (mokahr.com) scraper."""

from __future__ import annotations

import base64
import json

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import MokaScraper, ScraperRegistry
from ats_scrapers.scrapers.moka import decrypt_moka

URL_APP = "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2"
URL_HIRE_R1 = "https://hire-r1.mokahr.com/api/outer/ats-apply/website/jobs/v2"
KEY = "1234567890abcdef"
EMPTY_CIPHER = (
    "EzXcB1Inxi9rKIuYZb9XL/cjLEe0wCdwOB+BD8ot+uNfzl3LzdmG/Mkj+ipAdC9j9"
    "foq9xMFpcVF+u4d9emRrP10dylE9SKm/c2pxowFTJU="
)


def job(job_id: str = "job-1", title: str = "Senior Backend Engineer") -> dict:
    return {
        "id": job_id,
        "title": title,
        "jobDescription": "<p>Build distributed systems.</p>",
        "locations": [
            {
                "cityName": "上海市",
                "provinceName": "上海",
                "country": "中国",
            }
        ],
        "publishedAt": "2026-04-30T18:29:13",
        "status": "open",
        "department": {"id": 1, "name": "Engineering"},
        "commitment": "全职",
        "multiLocale": '{"mainLocale":"zh-CN"}',
    }


def envelope(jobs: list[dict] | None = None, *, total: int | None = None) -> dict:
    items = [job()] if jobs is None else jobs
    return {
        "code": 0,
        "success": True,
        "data": {
            "jobStats": {"total": len(items) if total is None else total},
            "jobs": items,
        },
    }


def test_decrypt_moka_pinned_fixture() -> None:
    assert decrypt_moka(EMPTY_CIPHER, KEY) == envelope([])


def test_decrypt_moka_rejects_bad_base64() -> None:
    with pytest.raises(ScraperError, match="not valid base64"):
        decrypt_moka("not::base64", KEY)


def test_decrypt_moka_rejects_short_key() -> None:
    with pytest.raises(ScraperError, match="invalid byte length"):
        decrypt_moka(EMPTY_CIPHER, "short")


def test_decrypt_moka_rejects_truncated_ciphertext() -> None:
    raw = base64.b64decode(EMPTY_CIPHER)[:-1]
    with pytest.raises(ScraperError, match="not a multiple of 16"):
        decrypt_moka(base64.b64encode(raw).decode(), KEY)


def test_registry_resolves_moka() -> None:
    assert ScraperRegistry.get(ATSType.MOKA) is MokaScraper


@pytest.mark.parametrize(
    ("slug", "match"),
    [
        ("trip", "must be"),
        ("trip/not-a-number", "siteId must be numeric"),
        ("trip/70415/internal", "recruitment type"),
        ("trip/70415/social/extra", "unexpected segments"),
    ],
)
def test_rejects_invalid_slugs(slug: str, match: str) -> None:
    with pytest.raises(ScraperError, match=match):
        MokaScraper(slug)


def test_parses_host_and_recruitment_type() -> None:
    scraper = MokaScraper("hire-r1/klookcareers/100008011/campus")
    assert scraper.slug == "klookcareers"
    assert scraper.site_id == 100008011
    assert scraper.host == "hire-r1.mokahr.com"
    assert scraper.recruitment_type == "campus"


def test_rejects_unknown_host_override() -> None:
    with pytest.raises(ScraperError, match="Unsupported Moka host"):
        MokaScraper("trip/70415", host="evil.example")


def test_parses_plaintext_envelope_end_to_end(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope())
    [result] = MokaScraper("fixturecorp/12345").fetch()

    assert result.ats_type is ATSType.MOKA
    assert result.ats_id == "job-1"
    assert result.global_id == "moka:job-1"
    assert result.title == "Senior Backend Engineer"
    assert result.company == "fixturecorp"
    assert result.location == "上海市, 上海, 中国"
    assert result.country_iso == "CN"
    assert result.region == "Asia"
    assert result.language == "zh"
    assert result.employment_type == "FULL_TIME"
    assert result.department == "Engineering"
    assert result.description == "Build distributed systems."
    assert result.posted_at is not None and result.posted_at.tzinfo is not None
    assert str(result.url) == (
        "https://app.mokahr.com/social-recruitment/fixturecorp/12345/job/job-1"
    )
    assert result.raw == {
        "slug": "fixturecorp",
        "site_id": 12345,
        "host": "app.mokahr.com",
        "status": "open",
    }


def test_decrypts_encrypted_envelope_end_to_end(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"data": EMPTY_CIPHER, "necromancer": KEY},
    )
    assert MokaScraper("fixturecorp/12345").fetch() == []


def test_host_prefix_targets_hire_r1(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_HIRE_R1, json=envelope([]))
    scraper = MokaScraper("hire-r1/klookcareers/100008011")
    assert scraper.fetch() == []
    [request] = httpx_mock.get_requests()
    assert request.headers["referer"] == (
        "https://hire-r1.mokahr.com/social-recruitment/klookcareers/100008011"
    )


def test_company_not_found_when_api_rejects_tenant(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"code": 703002, "msg": "未找到对应的官网", "success": False},
    )
    with pytest.raises(CompanyNotFoundError):
        MokaScraper("missing/99999").fetch()


def test_other_api_error_is_scraper_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL_APP,
        json={"code": 500001, "msg": "boom", "success": False},
    )
    with pytest.raises(ScraperError, match="boom"):
        MokaScraper("fixturecorp/12345").fetch()


def test_paginates_and_deduplicates_jobs(httpx_mock) -> None:
    first = [job(f"job-{index}", f"Engineer {index}") for index in range(50)]
    second = [job("job-49", "Engineer 49"), job("job-50", "Engineer 50")]
    httpx_mock.add_response(url=URL_APP, json=envelope(first, total=51))
    httpx_mock.add_response(url=URL_APP, json=envelope(second, total=51))

    jobs = MokaScraper("fixturecorp/12345").fetch()

    assert len(jobs) == 51
    assert jobs[-1].ats_id == "job-50"
    requests = httpx_mock.get_requests()
    assert json.loads(requests[0].content)["offset"] == 0
    assert json.loads(requests[1].content)["offset"] == 50


def test_request_body_matches_public_api(httpx_mock) -> None:
    httpx_mock.add_response(url=URL_APP, json=envelope([]))
    MokaScraper("fixturecorp/12345").fetch()
    [request] = httpx_mock.get_requests()
    assert json.loads(request.content) == {
        "orgId": "fixturecorp",
        "siteId": 12345,
        "limit": 50,
        "offset": 0,
        "needStat": True,
        "site": "social",
    }
