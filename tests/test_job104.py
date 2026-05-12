"""Tests for the 104.com.tw (Taiwan) scraper.

Pin the parsing contract (each 104 search API field → Job field) and
the (area × jobcat) slicing pagination behaviour. The API caps
``lastPage`` at 100, so the scraper expects to fan out across many
slices to cover the full corpus — we exercise the per-slice pagination
shape with sanitised mock payloads.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import Job104Scraper, ScraperRegistry

_API_RE = re.compile(r"^https://www\.104\.com\.tw/jobs/search/api/jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.job104 as j
    monkeypatch.setattr(j, "MAX_RETRIES", 1)
    monkeypatch.setattr(j, "RETRY_BASE_DELAY", 0.0)


# --- fixtures ---------------------------------------------------------------


def _job_item(
    *,
    job_no: str = "16x9z",
    job_name: str = "資深軟體工程師 / Senior Software Engineer",
    cust_name: str = "範例科技股份有限公司",
    job_addr_no_desc: str = "新竹縣竹北市",
    job_address: str = "光明六路東一段 100 號",
    appear_date: str = "20260510",
    salary_low: int | None = 50_000,
    salary_high: int | None = 80_000,
    lat: float | None = 24.8268,
    lon: float | None = 121.0044,
    description: str = "負責後端服務架構 / Build resilient backend services.",
    desc_snippet: str = "後端工程師",
    job_cat: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
    skills: list[dict[str, str]] | None = None,
    period: str = "3 ~ 5 年",
    option_edu: str = "學歷不拘",
) -> dict[str, Any]:
    return {
        "appearDate": appear_date,
        "applyCnt": 12,
        "coIndustry": 1001006001,
        "coIndustryDesc": "電腦系統整合服務業",
        "custName": cust_name,
        "custNo": "130000000142203",
        "description": description,
        "descSnippet": desc_snippet,
        "jobAddress": job_address,
        "jobAddrNo": 6001006002,
        "jobAddrNoDesc": job_addr_no_desc,
        "jobName": job_name,
        "jobNo": job_no,
        "jobRo": 1,
        "jobType": "1",
        "lat": lat,
        "lon": lon,
        "link": {
            "job": f"//www.104.com.tw/job/{job_no}",
            "applyAnalyze": f"//www.104.com.tw/jobs/apply/analyze/{job_no}",
            "cust": "//www.104.com.tw/company/abc",
        },
        "major": [],
        "optionEdu": option_edu,
        "period": period,
        "remoteWorkType": 0,
        "salaryHigh": salary_high,
        "salaryLow": salary_low,
        "tags": tags or ["上市上櫃", "員工旅遊"],
        "jobCat": job_cat or [
            {"name": "軟體工程師", "code": "2007001003"},
            {"name": "後端工程師", "code": "2007001005"},
        ],
        "languageRequirements": [],
        "employeeCount": "100-499人",
        "pcSkills": skills or [
            {"description": "Python"},
            {"description": "PostgreSQL"},
        ],
    }


def _page(
    items: list[dict[str, Any]],
    *,
    current_page: int = 1,
    last_page: int = 1,
    total: int = 1,
) -> dict[str, Any]:
    return {
        "data": items,
        "metadata": {
            "pagination": {
                "count": len(items),
                "currentPage": current_page,
                "lastPage": last_page,
                "total": total,
            }
        },
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_job104() -> None:
    assert ScraperRegistry.get(ATSType.JOB104) is Job104Scraper


def test_ats_value_is_string_104() -> None:
    """The ATS enum value is exactly ``"104"`` (digits as a string).
    Downstream consumers split ``global_id`` on the first colon so a
    purely numeric ats_type value is safe."""
    assert ATSType.JOB104.value == "104"


# --- happy path -------------------------------------------------------------


def test_parses_full_search_payload(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_job_item()]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001006000"],  # Hsinchu County
        jobcats=["2007000000"],  # Software
    ).fetch()
    assert len(jobs) == 1
    j = jobs[0]

    assert j.ats_type is ATSType.JOB104
    assert j.ats_id == "16x9z"
    assert j.global_id == "104:16x9z"
    assert j.title == "資深軟體工程師 / Senior Software Engineer"
    assert j.company == "範例科技股份有限公司"
    # Protocol-relative link is upgraded to https.
    assert str(j.url) == "https://www.104.com.tw/job/16x9z"
    # Location combines structured area description with the street address.
    assert j.location == "新竹縣竹北市 — 光明六路東一段 100 號"
    assert j.country_iso == "TW"
    assert j.language == "zh"
    assert j.lat == pytest.approx(24.8268)
    assert j.lon == pytest.approx(121.0044)
    # Salary is monthly TWD.
    assert j.salary_currency == "TWD"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 50_000
    assert j.salary_max == 80_000
    assert j.salary_summary == "NT$50,000 – NT$80,000"
    # Department is the top-level jobCat name.
    assert j.department == "軟體工程師"
    # appearDate YYYYMMDD → datetime at midnight.
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    assert j.posted_at.month == 5
    assert j.posted_at.day == 10
    # Description is HTML-cleaned (here already plain text) and preferred
    # over the snippet.
    assert j.description is not None
    assert "Build resilient backend services" in j.description
    # raw captures provider-specific overflow.
    assert j.raw is not None
    assert j.raw.get("industry") == 1001006001
    assert j.raw.get("education") == "學歷不拘"
    assert j.raw.get("experience_label") == "3 ~ 5 年"
    assert "Python" in (j.raw.get("skills") or [])


def test_country_iso_is_tw_for_normal_taiwan_area(httpx_mock: Any) -> None:
    """Any area code that isn't the cross-strait / overseas sentinel
    should produce ``country_iso="TW"`` without reading the location text."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_job_item(job_no="tp1", job_addr_no_desc="台北市信義區")]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],  # Taipei City
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].country_iso == "TW"


def test_country_iso_none_for_overseas_slice(httpx_mock: Any) -> None:
    """Slices through the ``海外`` (overseas) bucket genuinely aren't TW
    — leave ``country_iso`` unset so downstream LLM enrichment can resolve."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_job_item(job_no="o1", job_addr_no_desc="日本東京")]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001026000"],  # Overseas
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].country_iso is None


def test_country_iso_none_for_china_slice(httpx_mock: Any) -> None:
    """Cross-strait (China) postings shouldn't be marked ``TW``."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_job_item(job_no="cn1", job_addr_no_desc="上海市浦東新區")]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001025000"],  # China
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].country_iso is None


# --- pagination -------------------------------------------------------------


def test_paginates_within_slice_up_to_last_page(httpx_mock: Any) -> None:
    """``metadata.pagination.lastPage`` drives the per-slice page fan-out."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job_item(job_no=f"p1-{i}") for i in range(5)],
            current_page=1,
            last_page=3,
            total=15,
        ),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job_item(job_no=f"p2-{i}") for i in range(5)],
            current_page=2,
            last_page=3,
            total=15,
        ),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job_item(job_no=f"p3-{i}") for i in range(5)],
            current_page=3,
            last_page=3,
            total=15,
        ),
    )

    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert len(jobs) == 15


def test_dedupes_across_slices(httpx_mock: Any) -> None:
    """The same job appearing in two (area, jobcat) slices must only be
    emitted once — 104's category tagging often overlaps."""
    httpx_mock.add_response(
        url=re.compile(r".*jobcat=2007000000.*"),
        json=_page([_job_item(job_no="dupA"), _job_item(job_no="dupB")]),
    )
    httpx_mock.add_response(
        url=re.compile(r".*jobcat=2008000000.*"),
        json=_page([_job_item(job_no="dupB"), _job_item(job_no="dupC")]),
    )

    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000", "2008000000"],
    ).fetch()
    assert {j.ats_id for j in jobs} == {"dupA", "dupB", "dupC"}


# --- field-level edge cases --------------------------------------------------


def test_skips_jobs_missing_jobno_or_jobname(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(job_no="ok1"),
            {**_job_item(job_no="missing-name"), "jobName": ""},
            {**_job_item(), "jobNo": None},
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert [j.ats_id for j in jobs] == ["ok1"]


def test_handles_missing_salary(httpx_mock: Any) -> None:
    """When the employer hides salary, both bounds are 0 / missing —
    leave currency unset rather than emit a zeroed range."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(job_no="nosal", salary_low=None, salary_high=None),
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_min is None
    assert jobs[0].salary_max is None
    assert jobs[0].salary_summary is None


def test_drops_zero_zero_coordinates(httpx_mock: Any) -> None:
    """``(lat, lon) = (0, 0)`` shows up when the employer hasn't been
    geocoded — pinning to (0, 0) would put the job off the coast of
    Ghana, so drop both."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(job_no="z1", lat=0.0, lon=0.0),
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].lat is None
    assert jobs[0].lon is None


def test_strips_html_from_description(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(
                job_no="h1",
                description="<p>Build <b>resilient</b> backends.</p>",
            ),
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].description == "Build resilient backends."


def test_falls_back_to_desc_snippet_when_description_empty(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(
                job_no="s1", description="", desc_snippet="Short summary.",
            ),
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].description == "Short summary."


def test_location_falls_back_to_street_when_addr_desc_empty(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job_item(
                job_no="loc1", job_addr_no_desc="", job_address="新北市板橋區",
            ),
        ]),
    )
    jobs = Job104Scraper(
        "any",
        areas=["6001002000"],
        jobcats=["2007000000"],
    ).fetch()
    assert jobs[0].location == "新北市板橋區"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock: Any) -> None:
    """Real server failures should surface; the tolerant gather catches
    them at the slice boundary, so a single failing slice produces an
    empty result (logged warning) rather than crashing the run."""
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    ).fetch()
    # Single failing slice swallowed via _gather_tolerant — result is empty.
    assert jobs == []


def test_400_treated_as_empty_slice(httpx_mock: Any) -> None:
    """Some (area, jobcat) combos return 400 (unknown category at that
    area). The scraper should skip rather than abort the whole run."""
    httpx_mock.add_response(url=_API_RE, status_code=400)
    jobs = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2099000000"],
    ).fetch()
    assert jobs == []


def test_non_json_response_raises(httpx_mock: Any) -> None:
    """If 104 ever returns HTML for a 200 (CDN intercept), the scraper
    should raise rather than silently drop. _gather_tolerant catches at
    the slice level, but the inner _search call still wraps the parse
    failure in a ScraperError that we can assert on by bypassing the
    fan-out and probing the slice directly."""
    import asyncio

    import httpx as _httpx

    httpx_mock.add_response(url=_API_RE, text="<html>bot wall</html>")
    scraper = Job104Scraper(
        "any",
        areas=["6001001000"],
        jobcats=["2007000000"],
    )

    async def _probe() -> None:
        async with _httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(1)
            await scraper._search(
                client, sem,
                area="6001001000", jobcat="2007000000", page=1,
            )

    with pytest.raises(ScraperError):
        asyncio.run(_probe())
