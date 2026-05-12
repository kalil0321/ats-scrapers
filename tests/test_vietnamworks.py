"""Tests for the VietnamWorks scraper.

Pin the parsing contract (each VietnamWorks ``/job-search/v1.0/search``
field → ``Job`` field) and the page-based pagination behaviour. The API
clamps per-page size on the server, so the test suite mirrors that —
asserting that we walk pages until the server returns a short batch.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, VietnamWorksScraper
from jobhive.scrapers.vietnamworks import VietnamWorksScraper as _Scraper

_API_RE = re.compile(r"^https://ms\.vietnamworks\.com/job-search/v1\.0/search")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.vietnamworks as v

    monkeypatch.setattr(v, "MAX_RETRIES", 1)
    monkeypatch.setattr(v, "RETRY_BASE_DELAY", 0.0)


# A trimmed but realistic single job, captured live from
# ``POST ms.vietnamworks.com/job-search/v1.0/search`` with
# ``{"keyword":"","page":1,"size":2}``. Used as the structural fixture
# the parsing tests pin against.
_JOB_USD_VISIBLE: dict[str, Any] = {
    "jobId": 2043697,
    "jobTitle": "Recruitment Consultant (Industrial Manufacturing)",
    "jobUrl": (
        "https://www.vietnamworks.com/"
        "recruitment-consultant-industrial-manufacturing--2043697-jv"
    ),
    "alias": "recruitment-consultant-industrial-manufacturing-",
    "createdOn": "2026-04-21T17:25:42+07:00",
    "approvedOn": "2026-04-22T10:44:38+07:00",
    "expiredOn": "2026-05-31T23:59:59+07:00",
    "companyName": "Navigos Group",
    "companyId": 18364,
    "isOnline": True,
    "isActive": True,
    "isApproved": True,
    "isSalaryVisible": True,
    "jobDescription": (
        "<p>Navigos Search, a part of Navigos Group, is the No.1 provider"
        " of executive search services in Vietnam.</p>"
    ),
    "jobRequirement": (
        "<p>• Professionals with at least 2 years of experience in"
        " Headhunting.</p>"
    ),
    "jobLevelId": 5,
    "salary": 700,
    "salaryMax": 0,
    "salaryMin": 700,
    "salaryCurrency": "USD",
    "salaryPeriodId": 1,
    "typeWorkingId": 1,
    "workingLocations": [
        {
            "workingLocationId": 184409,
            "addressId": 144172,
            "cityId": 29,
            "districtId": 0,
            "address": "20th Floor - 11 Doan Van Bo",
            "cityName": "Ho Chi Minh",
            "cityNameVI": "Hồ Chí Minh",
        }
    ],
    "jobFunction": {
        "parentId": 12,
        "parentName": "Human Resources/Recruitment",
        "parentNameVI": "Nhân Sự/Tuyển Dụng",
        "children": [
            {"id": 76, "name": "Recruitment", "nameVI": "Tuyển Dụng"}
        ],
    },
    "yearsOfExperience": 2,
    "prettySalary": "Từ $ 700 /tháng",
}


# A job with the Vietnamese-titled "salary hidden" variant — both
# ``salaryMin`` and ``salaryMax`` zero and ``isSalaryVisible`` false.
_JOB_VI_HIDDEN: dict[str, Any] = {
    "jobId": 2045604,
    "jobTitle": "HO - Giám Đốc Bộ Phận Tài Chính Khối Kinh Doanh",
    "jobUrl": (
        "https://www.vietnamworks.com/"
        "ho-giam-doc-bo-phan-tai-chinh-khoi-kinh-doanh-2045604-jv"
    ),
    "alias": "ho-giam-doc-bo-phan-tai-chinh-khoi-kinh-doanh",
    "createdOn": "2026-04-24T14:01:07+07:00",
    "approvedOn": "2026-04-24T15:23:27+07:00",
    "expiredOn": "2026-06-02T23:59:59+07:00",
    "companyName": "Ngân Hàng TMCP Á Châu (ACB)",
    "companyId": 849,
    "isOnline": True,
    "isActive": True,
    "isApproved": True,
    "isSalaryVisible": False,
    "jobDescription": "<p><strong>1. Hỗ trợ chiến lược kinh doanh.</strong></p>",
    "jobRequirement": "<p>Kiến thức chuyên môn.</p>",
    "jobLevelId": 3,
    "salary": 0,
    "salaryMax": 0,
    "salaryMin": 0,
    "salaryCurrency": "USD",
    "salaryPeriodId": 1,
    "typeWorkingId": 1,
    "workingLocations": [
        {
            "cityName": "Ho Chi Minh",
            "cityNameVI": "Hồ Chí Minh",
        }
    ],
    "jobFunction": {
        "parentName": "Banking & Financial Services",
        "children": [{"name": "Financial Analysis & Research"}],
    },
    "yearsOfExperience": 5,
    "prettySalary": "Thương lượng",
}


def _page(
    items: list[dict[str, Any]],
    *,
    nb_hits: int | None = None,
    nb_pages: int | None = None,
) -> dict[str, Any]:
    """Build a mock VietnamWorks page response. ``nb_hits`` / ``nb_pages``
    drive the scraper's pagination when set; leave them out to simulate
    a stripped response."""
    meta: dict[str, Any] = {"code": 200, "message": "Success"}
    if nb_hits is not None:
        meta["nbHits"] = nb_hits
    if nb_pages is not None:
        meta["nbPages"] = nb_pages
    return {"meta": meta, "data": items, "facets": {}}


# --- registry / wiring -------------------------------------------------------


def test_registry_resolves_vietnamworks() -> None:
    assert ScraperRegistry.get(ATSType.VIETNAMWORKS) is VietnamWorksScraper


def test_ats_attribute_matches_enum() -> None:
    assert VietnamWorksScraper.ats is ATSType.VIETNAMWORKS


# --- parsing -----------------------------------------------------------------


def test_parses_full_payload_with_visible_salary() -> None:
    scraper = _Scraper("any")
    job = scraper._parse_job(_JOB_USD_VISIBLE)
    assert job is not None
    assert job.ats_type is ATSType.VIETNAMWORKS
    assert job.ats_id == "2043697"
    assert job.title.startswith("Recruitment Consultant")
    assert job.company == "Navigos Group"
    assert str(job.url).startswith("https://www.vietnamworks.com/")
    assert job.country_iso == "VN"
    assert job.location == "Ho Chi Minh"
    # Salary: ``salaryMin=700, salaryMax=0`` — the parser should fall
    # back to ``salary`` for the upper bound when max is unset, but
    # currency and min must always survive.
    assert job.salary_currency == "USD"
    assert job.salary_min == 700.0
    assert job.salary_period == "MONTH"
    assert job.employment_type == "FULL_TIME"
    assert job.experience == 2
    assert job.language == "en"
    assert job.department == "Human Resources/Recruitment"
    assert job.team == "Recruitment"
    # Description has HTML stripped + entities decoded.
    assert job.description is not None
    assert "<p>" not in job.description
    assert "Navigos Search" in job.description
    assert job.posted_at is not None
    assert job.fetched_at is not None
    assert job.global_id == "vietnamworks:2043697"
    assert job.raw is not None
    assert job.raw.get("company_id") == 18364
    assert job.raw.get("alias") == "recruitment-consultant-industrial-manufacturing-"


def test_parses_vietnamese_title_marks_language_vi() -> None:
    job = _Scraper("any")._parse_job(_JOB_VI_HIDDEN)
    assert job is not None
    assert job.language == "vi"
    # ``isSalaryVisible=False`` ⇒ scraper drops salary fields entirely.
    assert job.salary_currency is None
    assert job.salary_min is None
    assert job.salary_max is None
    # ``prettySalary`` is still preserved as a salary_summary so the
    # downstream consumer doesn't lose the "Thương lượng" (Negotiable)
    # signal.
    assert job.salary_summary == "Thương lượng"


def test_skips_jobs_missing_id_or_title() -> None:
    """Defensive: drop malformed entries rather than emit half-built rows."""
    scraper = _Scraper("any")
    assert scraper._parse_job({"jobTitle": "No id"}) is None
    assert scraper._parse_job({"jobId": 1, "jobTitle": ""}) is None
    assert scraper._parse_job({"jobId": 1}) is None


def test_synthesizes_url_when_joburl_missing() -> None:
    """The contract calls for synthesizing the canonical URL from
    ``jobId`` when the API omits ``jobUrl`` (defensive path)."""
    job = _Scraper("any")._parse_job({
        "jobId": 999,
        "jobTitle": "Test Role",
        "companyName": "Acme",
    })
    assert job is not None
    assert "999" in str(job.url)
    # When no description/requirement is present, fall back to the title.
    assert job.description == "Test Role"


def test_location_falls_back_to_vietnamese_city_name() -> None:
    """When ``cityName`` (English) is empty, use ``cityNameVI``."""
    job = _Scraper("any")._parse_job({
        "jobId": 1,
        "jobTitle": "Engineer",
        "companyName": "Acme",
        "workingLocations": [{"cityNameVI": "Hà Nội"}],
    })
    assert job is not None
    assert job.location == "Hà Nội"


# --- pagination --------------------------------------------------------------


def test_walks_pages_until_meta_nb_pages(httpx_mock) -> None:
    """``meta.nbPages`` drives the page sweep — walk 1..nbPages."""
    # Server clamps page size to 10; mirror that here.
    page1 = _page(
        [{**_JOB_USD_VISIBLE, "jobId": i} for i in range(10)],
        nb_hits=15, nb_pages=2,
    )
    page2 = _page(
        [{**_JOB_USD_VISIBLE, "jobId": i} for i in range(10, 15)],
        nb_hits=15, nb_pages=2,
    )
    httpx_mock.add_response(url=_API_RE, method="POST", json=page1)
    httpx_mock.add_response(url=_API_RE, method="POST", json=page2)

    jobs = VietnamWorksScraper("any").fetch()
    assert len(jobs) == 15
    assert {j.ats_id for j in jobs} == {str(i) for i in range(15)}


def test_dedupes_overlapping_pages(httpx_mock) -> None:
    """If a page repeats a tail item, collapse them on ``ats_id``."""
    page1 = _page(
        [{**_JOB_USD_VISIBLE, "jobId": i} for i in range(10)],
        nb_hits=15, nb_pages=2,
    )
    page2 = _page(
        # Re-includes jobIds 8, 9 plus new ones.
        [{**_JOB_USD_VISIBLE, "jobId": i} for i in [8, 9, 10, 11, 12]],
        nb_hits=15, nb_pages=2,
    )
    httpx_mock.add_response(url=_API_RE, method="POST", json=page1)
    httpx_mock.add_response(url=_API_RE, method="POST", json=page2)

    jobs = VietnamWorksScraper("any").fetch()
    assert len({j.ats_id for j in jobs}) == 13


def test_empty_first_page_returns_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE, method="POST", json=_page([], nb_hits=0, nb_pages=0),
    )
    assert VietnamWorksScraper("any").fetch() == []


def test_out_of_range_page_with_null_payload(httpx_mock) -> None:
    """Out-of-range pages echo back ``{"meta": null, "data": null}``.
    The scraper should normalise that to an empty result without
    crashing the run."""
    httpx_mock.add_response(
        url=_API_RE, method="POST", json={"meta": None, "data": None},
    )
    assert VietnamWorksScraper("any").fetch() == []


# --- error handling ----------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """Server-side failures must surface, not silently emit []."""
    httpx_mock.add_response(
        url=_API_RE, method="POST", status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        VietnamWorksScraper("any").fetch()
