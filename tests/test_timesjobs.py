"""Tests for the TimesJobs scraper.

The API is a page-based JSON POST endpoint at
``tjapi.timesjobs.com/search/api/v1/search/jobs/list`` keyed by a
single-space wildcard keyword. We pin: registry wiring, pagination via
``totalPages``, the salary ``-1`` sentinel, India-specific country
inference, skill/experience extraction into ``raw``, and the standard
retry / shape-changed defensive paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, TimesJobsScraper

API_URL = "https://tjapi.timesjobs.com/search/api/v1/search/jobs/list"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tighten retry timings so test runs stay snappy."""
    import ats_scrapers.scrapers.timesjobs as tj

    monkeypatch.setattr(tj, "MAX_RETRIES", 1)
    monkeypatch.setattr(tj, "RETRY_BASE_DELAY", 0.0)


def _job(
    job_id: str = "80497778",
    *,
    title: str = "QA Automation Engineer",
    company: str = "Acme",
    location: str = "Bengaluru",
    description: str = "Build automated tests.",
    low_salary: int = -1,
    high_salary: int = -1,
    currency: str = "INR",
    skills: str = "Selenium, Java, Pytest",
    experience_from: int = 3,
    experience_to: int = 6,
    job_type: str = "On-site",
    job_function: str = "IT Software : QA & Testing",
    post_date: str = "2026-05-11",
    job_detail_url: str | None = None,
) -> dict[str, Any]:
    return {
        "jobId": job_id,
        "title": title,
        "company": company,
        "hfCompany": company,
        "location": location,
        "description": description,
        "lowSalary": low_salary,
        "highSalary": high_salary,
        "currency": currency,
        "skills": skills,
        "experienceFrom": experience_from,
        "experienceTo": experience_to,
        "jobType": job_type,
        "jobFunction": job_function,
        "postDate": post_date,
        "expiryDate": "2026-07-09",
        "jobDetailUrl": (
            job_detail_url
            or f"https://www.timesjobs.com/job-detail/job-{job_id}"
        ),
    }


def _response(
    jobs: list[dict[str, Any]],
    *,
    page: int = 1,
    total_pages: float | int = 1,
    total: int | None = None,
) -> dict[str, Any]:
    return {
        "total": total if total is not None else len(jobs),
        "page": page,
        "size": len(jobs),
        # The real API returns totalPages as a float (e.g. 1978.0).
        "totalPages": total_pages,
        "jobs": jobs,
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_timesjobs() -> None:
    assert ScraperRegistry.get(ATSType.TIMESJOBS) is TimesJobsScraper


def test_ats_type_value() -> None:
    assert ATSType.TIMESJOBS.value == "timesjobs"


# --- happy path -------------------------------------------------------------


def test_parses_minimal_job(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=_response([_job()]))
    jobs = TimesJobsScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.TIMESJOBS
    assert j.ats_id == "80497778"
    assert j.global_id == "timesjobs:80497778"
    assert j.title == "QA Automation Engineer"
    assert j.company == "Acme"
    assert j.location == "Bengaluru"
    assert j.country_iso == "IN"
    assert j.language == "en"
    assert j.description == "Build automated tests."
    assert str(j.url).startswith("https://www.timesjobs.com/job-detail/")


def test_paginates_via_total_pages(httpx_mock) -> None:
    """Page 1 is fetched synchronously to learn the page count, then
    remaining pages fan out concurrently."""
    httpx_mock.add_response(
        url=API_URL,
        match_json={
            "keyword": " ", "location": "", "page": "1", "size": "100",
        },
        json=_response(
            [_job(job_id="1"), _job(job_id="2")],
            page=1,
            total_pages=3.0,
            total=6,
        ),
    )
    httpx_mock.add_response(
        url=API_URL,
        match_json={
            "keyword": " ", "location": "", "page": "2", "size": "100",
        },
        json=_response([_job(job_id="3")], page=2, total_pages=3.0, total=6),
    )
    httpx_mock.add_response(
        url=API_URL,
        match_json={
            "keyword": " ", "location": "", "page": "3", "size": "100",
        },
        json=_response([_job(job_id="4")], page=3, total_pages=3.0, total=6),
    )
    jobs = TimesJobsScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3", "4"]


def test_dedupes_repeated_job_ids(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response(
            [_job(job_id="1"), _job(job_id="1"), _job(job_id="2")],
        ),
    )
    jobs = TimesJobsScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2"]


# --- field extraction -------------------------------------------------------


def test_salary_minus_one_is_treated_as_missing(httpx_mock) -> None:
    """TimesJobs encodes 'salary not disclosed' as low/high = -1. Make
    sure we don't emit a -1 salary range."""
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(low_salary=-1, high_salary=-1)]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.salary_min is None
    assert j.salary_max is None
    assert j.salary_currency is None


def test_real_salary_range_is_preserved(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([
            _job(low_salary=800_000, high_salary=1_500_000, currency="INR"),
        ]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.salary_min == 800_000
    assert j.salary_max == 1_500_000
    assert j.salary_currency == "INR"


def test_currency_alias_rs_is_normalized_to_inr(httpx_mock) -> None:
    """A handful of rows use 'RS' instead of the ISO 'INR' code."""
    httpx_mock.add_response(
        url=API_URL,
        json=_response([
            _job(low_salary=500_000, high_salary=900_000, currency="RS"),
        ]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.salary_currency == "INR"


def test_skills_split_into_raw_list(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([
            _job(skills="Selenium Automation, Java Programming, Test Frameworks"),
        ]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["skills"] == [
        "Selenium Automation", "Java Programming", "Test Frameworks",
    ]


def test_experience_label_in_raw(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([
            _job(experience_from=5, experience_to=8),
        ]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.experience == 5
    assert j.raw is not None
    assert j.raw["experience_from"] == 5
    assert j.raw["experience_to"] == 8
    assert j.raw["experience_label"] == "5-8 years"


def test_experience_label_collapses_when_from_equals_to(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(experience_from=4, experience_to=4)]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["experience_label"] == "4 years"


def test_remote_job_type_sets_is_remote(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(job_type="Remote")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.is_remote is True
    assert j.commitment == "Remote"


def test_onsite_job_type_leaves_is_remote_none(httpx_mock) -> None:
    """Per the model contract is_remote only ever asserts True — absence
    of a remote marker should not be encoded as False."""
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(job_type="On-site")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.is_remote is None
    assert j.commitment == "On-site"


def test_post_date_parsed_to_datetime(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(post_date="2026-05-11")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    assert j.posted_at.month == 5
    assert j.posted_at.day == 11


def test_html_is_stripped_from_description(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([
            _job(description="<p>Lead the <b>QA</b> team.</p>"),
        ]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.description is not None
    assert "<" not in j.description
    assert "QA" in j.description


# --- country_iso inference --------------------------------------------------


def test_country_iso_in_for_clearly_indian_location(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(location="Bengaluru")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.country_iso == "IN"


def test_country_iso_none_for_global_location(httpx_mock) -> None:
    """TimesJobs indexes global postings too — stamping every row with
    IN would corrupt the country field. Leave it unset for non-Indian
    locations and let downstream LLM enrichment derive it."""
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(location="Taiwan")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.country_iso is None


def test_country_iso_none_for_mixed_location(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(location="Bengaluru, New York")]),
    )
    j = TimesJobsScraper("any").fetch()[0]
    assert j.country_iso is None


# --- defensive --------------------------------------------------------------


def test_skips_rows_missing_required_fields(httpx_mock) -> None:
    """Rows without jobId/title/jobDetailUrl are skipped rather than
    failing the whole page."""
    bad_no_id = _job()
    bad_no_id.pop("jobId")
    bad_no_title = _job(job_id="2")
    bad_no_title.pop("title")
    bad_no_url = _job(job_id="3")
    bad_no_url.pop("jobDetailUrl")
    good = _job(job_id="4")
    httpx_mock.add_response(
        url=API_URL,
        json=_response([bad_no_id, bad_no_title, bad_no_url, good]),
    )
    jobs = TimesJobsScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["4"]


def test_non_dict_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=["not", "a", "dict"])
    with pytest.raises(ScraperError, match="API shape changed"):
        TimesJobsScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        TimesJobsScraper("any").fetch()


def test_400_on_subsequent_page_terminates_cleanly(httpx_mock) -> None:
    """A 400 past the last page is the API's way of saying 'no more
    rows' — treat as exhausted, not as an error."""
    httpx_mock.add_response(
        url=API_URL,
        match_json={
            "keyword": " ", "location": "", "page": "1", "size": "100",
        },
        json=_response(
            [_job(job_id="1")], page=1, total_pages=2.0, total=2,
        ),
    )
    httpx_mock.add_response(
        url=API_URL,
        match_json={
            "keyword": " ", "location": "", "page": "2", "size": "100",
        },
        status_code=400,
        json={"error": "Invalid JSON format"},
    )
    jobs = TimesJobsScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_400_on_first_page_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, status_code=400)
    with pytest.raises(ScraperError, match="returned 400"):
        TimesJobsScraper("any").fetch()


def test_missing_total_pages_raises(httpx_mock) -> None:
    payload = _response([_job()])
    payload.pop("totalPages")
    httpx_mock.add_response(url=API_URL, json=payload)
    with pytest.raises(ScraperError, match="totalPages"):
        TimesJobsScraper("any").fetch()


def test_failed_later_page_raises_instead_of_returning_partial(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(job_id="1")], total_pages=2, total=2),
    )
    httpx_mock.add_response(url=API_URL, status_code=500)
    with pytest.raises(ScraperError, match="page=2"):
        TimesJobsScraper("any").fetch()


def test_country_iso_accepts_india_suffix(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=_response([_job(location="Bengaluru, Karnataka, India")]),
    )
    assert TimesJobsScraper("any").fetch()[0].country_iso == "IN"
