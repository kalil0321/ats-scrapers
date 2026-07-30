from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import NinehireScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "day1company"
BASE_URL = f"https://{TENANT}.ninehire.site"
COMPANY_ID = "70683bd0-612b-11ec-bd23-6b2cabce5a2f"
OTHER_COMPANY_ID = "11111111-2222-3333-4444-555555555555"
RECRUITMENT_ID = "fab5ece0-8bee-11f1-9802-bf2fa0054e80"
API_URL = "https://api.ninehire.com/identity-access/homepage/recruitments"


def _next_html(page_props: dict) -> str:
    payload = {"props": {"pageProps": page_props}}
    return (
        "<html><body><script id=\"__NEXT_DATA__\" "
        f"type=\"application/json\">{json.dumps(payload)}</script></body></html>"
    )


def _homepage(
    *,
    company_id: str = COMPANY_ID,
    info_company_id: str | None = None,
    status: str = "published",
    site_url: str = TENANT,
) -> str:
    return _next_html(
        {
            "homepageProps": {
                "homepage": {"companyId": company_id},
                "info": {
                    "companyId": info_company_id or company_id,
                    "companyName": "데이원컴퍼니",
                    "status": status,
                },
                "domain": {"siteUrl": site_url},
            }
        }
    )


def _job(
    index: int = 1,
    *,
    recruitment_id: str = RECRUITMENT_ID,
    company_id: str = COMPANY_ID,
    title: str = "[계약직] 세일즈 매니저",
    address_key: str = "7a0UlD8L",
) -> dict:
    return {
        "companyId": company_id,
        "recruitmentId": recruitment_id,
        "status": "in_progress",
        "title": title,
        "externalTitle": title,
        "addressKey": address_key,
        "deadlineValue": None,
        "deadlineType": "until_filled",
        "employmentType": ["contractor"],
        "career": {"type": "experienced", "range": {"over": 1, "below": 0}},
        "jobLocations": [
            {
                "x": 127.041154766578,
                "y": 37.5026496860008,
                "placeName": "센터필드 West",
                "addressName": "서울 강남구 테헤란로 231",
            }
        ],
        "jobGroup": {"title": "세일즈"},
        "jobTask": {"title": "B2C 세일즈"},
        "affiliation": {"title": "포도"},
        "tags": [{"content": "계약직"}, {"content": f"job-{index}"}],
        "createdAt": "2026-07-30T08:31:42.000Z",
        "alwaysExposure": False,
    }


def _page(results: list[dict], *, count: int | None = None) -> dict:
    return {
        "count": len(results) if count is None else count,
        "results": results,
    }


def _api_url(page: int = 1) -> str:
    return (
        f"{API_URL}?companyId={COMPANY_ID}&page={page}"
        "&countPerPage=100&order=created_at_desc"
    )


def _detail(
    *,
    recruitment_id: str = RECRUITMENT_ID,
    address_key: str = "7a0UlD8L",
    active: bool = True,
    content: str = "<h3>담당 업무</h3><p>고객 상담과 세일즈</p>",
) -> str:
    return _next_html(
        {
            "recruitment": {
                "recruitmentId": recruitment_id,
                "addressKey": address_key,
            },
            "jobPosting": {
                "isActive": active,
                "content": content,
            },
        }
    )


def _add_homepage(httpx_mock, html: str | None = None) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/",
        text=html or _homepage(),
    )


def test_registry_resolves_ninehire() -> None:
    assert ScraperRegistry.get(ATSType.NINEHIRE) is NinehireScraper


def test_fetches_structured_jobs(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(
        url=_api_url(),
        json=_page([_job()]),
    )

    job = NinehireScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()[0]

    assert job.ats_type is ATSType.NINEHIRE
    assert job.ats_id == RECRUITMENT_ID
    assert job.title == "[계약직] 세일즈 매니저"
    assert job.company == "데이원컴퍼니"
    assert str(job.url) == f"{BASE_URL}/job_posting/7a0UlD8L"
    assert str(job.apply_url) == f"{BASE_URL}/job_posting/7a0UlD8L"
    assert job.location == "서울 강남구 테헤란로 231"
    assert job.country_iso == "KR"
    assert job.region == "Asia"
    assert job.lat == pytest.approx(37.5026496860008)
    assert job.lon == pytest.approx(127.041154766578)
    assert job.experience == 1
    assert job.employment_type == "CONTRACT"
    assert job.commitment == "contractor"
    assert job.department == "세일즈"
    assert job.team == "B2C 세일즈"
    assert job.posted_at == datetime(
        2026, 7, 30, 8, 31, 42, tzinfo=UTC
    )
    assert job.fetched_at is not None
    assert job.language == "ko"
    assert job.raw["affiliation"] == "포도"
    assert job.raw["tags"] == ["계약직", "job-1"]


def test_catalog_company_name_overrides_homepage(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(url=_api_url(), json=_page([_job()]))

    job = NinehireScraper(
        TENANT,
        company_name="Day 1 Company",
        include_descriptions=False,
    ).fetch()[0]

    assert job.company == "Day 1 Company"


def test_filters_non_job_postings(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(
        url=_api_url(),
        json=_page(
            [
                _job(),
                _job(
                    2,
                    recruitment_id="11111111-2222-3333-4444-555555555551",
                    title="☕️ 커피챗 신청하기",
                    address_key="coffee01",
                ),
                _job(
                    3,
                    recruitment_id="11111111-2222-3333-4444-555555555552",
                    title="Talent Pool(상시 인재풀 등록)",
                    address_key="talent01",
                ),
                _job(
                    4,
                    recruitment_id="11111111-2222-3333-4444-555555555553",
                    title="최종 합격 발표 안내",
                    address_key="result01",
                ),
            ]
        ),
    )

    jobs = NinehireScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()

    assert [job.ats_id for job in jobs] == [RECRUITMENT_ID]


def test_paginates_to_exact_advertised_count(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    first_page = [
        _job(
            index,
            recruitment_id=f"00000000-0000-4000-8000-{index:012x}",
            address_key=f"job{index:04d}",
        )
        for index in range(100)
    ]
    last = _job(
        100,
        recruitment_id="00000000-0000-4000-8000-000000000100",
        address_key="job0100",
    )
    httpx_mock.add_response(
        url=_api_url(1),
        json=_page(first_page, count=101),
    )
    httpx_mock.add_response(
        url=_api_url(2),
        json=_page([last], count=101),
    )

    jobs = NinehireScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()

    assert len(jobs) == 101


def test_count_change_fails_closed(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    first_page = [
        _job(
            index,
            recruitment_id=f"00000000-0000-4000-8000-{index:012x}",
            address_key=f"job{index:04d}",
        )
        for index in range(100)
    ]
    httpx_mock.add_response(
        url=_api_url(1),
        json=_page(first_page, count=101),
    )
    httpx_mock.add_response(
        url=_api_url(2),
        json=_page([], count=102),
    )

    with pytest.raises(ScraperError, match="count changed"):
        NinehireScraper(TENANT, include_descriptions=False).fetch()


def test_duplicate_ids_fail_closed(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(
        url=_api_url(),
        json=_page([_job(), _job(2)]),
    )

    with pytest.raises(ScraperError, match="duplicate job ID"):
        NinehireScraper(TENANT, include_descriptions=False).fetch()


def test_cross_company_job_fails_closed(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(
        url=_api_url(),
        json=_page([_job(company_id=OTHER_COMPANY_ID)]),
    )

    with pytest.raises(ScraperError, match="another company's job"):
        NinehireScraper(TENANT, include_descriptions=False).fetch()


def test_default_fetch_hydrates_description(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(url=_api_url(), json=_page([_job()]))
    httpx_mock.add_response(
        url=f"{BASE_URL}/job_posting/7a0UlD8L",
        text=_detail(),
    )

    job = NinehireScraper(TENANT).fetch()[0]

    assert job.description == "담당 업무\n고객 상담과 세일즈"


def test_inactive_detail_returns_no_description(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(url=_api_url(), json=_page([_job()]))
    scraper = NinehireScraper(TENANT, include_descriptions=False)
    job = scraper.fetch()[0]
    httpx_mock.add_response(
        url=f"{BASE_URL}/job_posting/7a0UlD8L",
        text=_detail(active=False),
    )

    assert scraper.get_description(job) is None


def test_detail_identity_change_fails_closed(httpx_mock) -> None:
    _add_homepage(httpx_mock)
    httpx_mock.add_response(url=_api_url(), json=_page([_job()]))
    scraper = NinehireScraper(TENANT, include_descriptions=False)
    job = scraper.fetch()[0]
    httpx_mock.add_response(
        url=f"{BASE_URL}/job_posting/7a0UlD8L",
        text=_detail(
            recruitment_id="11111111-2222-3333-4444-555555555555"
        ),
    )

    with pytest.raises(ScraperError, match="detail returned ID"):
        scraper.get_description(job)


@pytest.mark.parametrize(
    ("homepage", "message"),
    [
        (_homepage(status="draft"), "not published"),
        (
            _homepage(info_company_id=OTHER_COMPANY_ID),
            "company IDs do not match",
        ),
        (_homepage(site_url="other"), "resolved to tenant"),
        ("<html>maintenance</html>", "no Next.js bootstrap"),
    ],
)
def test_rejects_invalid_homepage(
    httpx_mock,
    homepage: str,
    message: str,
) -> None:
    _add_homepage(httpx_mock, homepage)

    with pytest.raises(ScraperError, match=message):
        NinehireScraper(TENANT).fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "bad_slug",
        "../tenant",
        "tenant.ninehire.site",
        "https://tenant.ninehire.site",
        "a" * 64,
        "trailing-",
    ],
)
def test_rejects_untrusted_tenant_slugs(slug: str) -> None:
    with pytest.raises(ScraperError, match="NinehireScraper"):
        NinehireScraper(slug)
