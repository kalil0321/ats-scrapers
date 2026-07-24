"""Tests for the JobThai (jobthai.com) scraper.

Pin the parsing contract (GraphQL ``searchJobs`` payload → Job
fields), the ``jobtype`` sharding plan that defeats the
Elasticsearch ``from+size<=10000`` cap, and the salary-text parser
for the common Thai-baht / ``ตามตกลง`` (negotiable) patterns.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import JobThaiScraper, ScraperRegistry

_API_URL = "https://api.jobthai.com/v1/graphql"
_API_RE = re.compile(r"^https://api\.jobthai\.com/v1/graphql$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.jobthai as jt
    monkeypatch.setattr(jt, "MAX_RETRIES", 1)
    monkeypatch.setattr(jt, "RETRY_BASE_DELAY", 0.0)


def _row(
    *,
    job_id: int = 1900404,
    job_title: str = "เจ้าหน้าที่ฝ่ายผลิต",
    company_name: str = "บริษัท ฟงซาน อินเตอร์เนชั่นแนล (ไทยแลนด์) จำกัด",
    company_id: int = 221463,
    work_location: str = "",
    salary: str = "15,000 - 20,000 บาท",
    job_description: list[str] | None = None,
    updated_at: str = "2026-05-12T04:06:46.000Z",
    tags: list[str] | None = None,
    urgent_id: int = 0,
    job_type_id: int = 17,
    job_type_name: str = "งานผลิต/ควบคุมคุณภาพ/โรงงาน",
    province_id: str = "01",
    province_name: str = "กรุงเทพมหานคร",
    district_id: str = "0141",
    district_name: str = "วัฒนา",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "jobTitle": job_title,
        "companyName": company_name,
        "companyID": company_id,
        "workLocation": work_location,
        "salary": salary,
        "jobDescription": job_description if job_description is not None else [
            "ติดตามเร่งรัดหนี้สิน",
            "จัดทำรายงานประจำเดือน",
        ],
        "updatedAt": updated_at,
        "tags": tags if tags is not None else [],
        "urgent": {"id": urgent_id, "name": ""},
        "jobType": {"id": job_type_id, "name": job_type_name},
        "province": {"id": province_id, "name": province_name},
        "district": {"id": district_id, "name": district_name},
    }


def _envelope(rows: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    """Wrap rows in the same shape the GraphQL endpoint returns."""
    return {
        "data": {
            "searchJobs": {
                "data": {
                    "total": total if total is not None else len(rows),
                    "data": rows,
                },
            },
        },
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_jobthai() -> None:
    assert ScraperRegistry.get(ATSType.JOBTHAI) is JobThaiScraper


def test_ats_type_value() -> None:
    """Pin the enum value — downstream CSV columns key off this string."""
    assert ATSType.JOBTHAI.value == "jobthai"


# --- happy path -------------------------------------------------------------


def test_parses_full_search_payload(httpx_mock) -> None:
    """Single-bucket scrape; verify every populated Job field maps to
    the right ``searchJobs`` field."""
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([_row()]),
    )

    jobs = JobThaiScraper("any", job_type_ids=("17",)).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.JOBTHAI
    assert j.ats_id == "1900404"
    assert j.title == "เจ้าหน้าที่ฝ่ายผลิต"
    assert j.company.startswith("บริษัท ฟงซาน")
    assert j.country_iso == "TH"
    assert j.region == "Asia"
    # Title contains Thai script → language is "th"
    assert j.language == "th"
    # Location is district-first, then province (both in Thai)
    assert j.location == "วัฒนา, กรุงเทพมหานคร"
    # Salary parses to THB / MONTH and pulls min/max from the range
    assert j.salary_currency == "THB"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 15000
    assert j.salary_max == 20000
    assert j.salary_summary == "15,000 - 20,000 บาท"
    # Posted-at parses the trailing-Z ISO format
    assert j.posted_at is not None
    assert j.posted_at.year == 2026
    # Description joins the bullet list with newlines
    assert j.description is not None
    assert "ติดตามเร่งรัดหนี้สิน" in j.description
    assert "\n" in j.description
    # URL template uses the English locale
    assert str(j.url) == "https://www.jobthai.com/en/job/1900404"
    # Raw stores the structured taxonomy ids
    assert j.raw is not None
    assert j.raw["category"] == 17
    assert j.raw["province_id"] == "01"
    assert j.raw["district_id"] == "0141"
    assert j.raw["company_id"] == 221463


def test_english_only_title_sets_language_en(httpx_mock) -> None:
    """Postings with no Thai-script characters in the title should
    be tagged ``language="en"`` — rare but real (multinationals
    posting English-only titles)."""
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([_row(job_title="Investor Relations Officer")]),
    )
    jobs = JobThaiScraper("any", job_type_ids=("17",)).fetch()
    assert jobs[0].language == "en"


# --- pagination -------------------------------------------------------------


def test_paginates_until_short_page(httpx_mock) -> None:
    """``searchJobs`` uses offset pagination (``page``,``size``).
    Stop when a page returns < size rows."""
    import ats_scrapers.scrapers.jobthai as jt
    # Page 1: full page (PER_PAGE rows). Page 2: short page → stop.
    page_one = [_row(job_id=i) for i in range(jt.PER_PAGE)]
    page_two = [_row(job_id=i) for i in range(jt.PER_PAGE, jt.PER_PAGE + 5)]
    httpx_mock.add_response(url=_API_URL, json=_envelope(page_one))
    httpx_mock.add_response(url=_API_URL, json=_envelope(page_two))

    jobs = JobThaiScraper("any", job_type_ids=("4",)).fetch()
    assert len(jobs) == jt.PER_PAGE + 5


def test_dedupes_overlapping_buckets(httpx_mock) -> None:
    """A job tagged with multiple ``jobType`` IDs will be returned
    once per bucket sweep. Dedup must collapse them on ``ats_id``."""
    # Two buckets, both returning the same job.
    httpx_mock.add_response(url=_API_URL, json=_envelope([_row(job_id=1)]))
    httpx_mock.add_response(url=_API_URL, json=_envelope([_row(job_id=1)]))

    jobs = JobThaiScraper("any", job_type_ids=("4", "11")).fetch()
    assert len(jobs) == 1
    assert jobs[0].ats_id == "1"


def test_empty_first_page_short_circuits(httpx_mock) -> None:
    """If a jobtype has zero postings the first page returns an empty
    ``data`` array — don't make a second request."""
    httpx_mock.add_response(url=_API_URL, json=_envelope([]))
    jobs = JobThaiScraper("any", job_type_ids=("99",)).fetch()
    assert jobs == []
    # Exactly one HTTP request was made (the empty first page).
    assert len(httpx_mock.get_requests()) == 1


# --- salary parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "salary_text,expected_min,expected_max,expected_currency",
    [
        ("15,000 - 20,000 บาท", 15000, 20000, "THB"),
        ("35,000 - 50,000 บาท", 35000, 50000, "THB"),
        # En-dash separator also seen on the API.
        ("18,000 – 22,000 บาท", 18000, 22000, "THB"),
        # Single value with trailing baht marker → min == max.
        ("25,000 บาท", 25000, 25000, "THB"),
        # "Negotiable" phrases — currency stays None.
        ("ตามตกลง", None, None, None),
        ("ตามโครงสร้างบริษัทฯ", None, None, None),
    ],
)
def test_salary_parser(
    httpx_mock,
    salary_text: str,
    expected_min: float | None,
    expected_max: float | None,
    expected_currency: str | None,
) -> None:
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([_row(salary=salary_text)]),
    )
    jobs = JobThaiScraper("any", job_type_ids=("17",)).fetch()
    assert jobs[0].salary_summary == salary_text
    assert jobs[0].salary_min == expected_min
    assert jobs[0].salary_max == expected_max
    assert jobs[0].salary_currency == expected_currency


# --- location handling ------------------------------------------------------


def test_location_falls_back_to_work_location_first_line(httpx_mock) -> None:
    """When the structured ``province``/``district`` are missing,
    fall back to the first line of the free-text ``workLocation``."""
    row = _row()
    row["province"] = {"id": None, "name": ""}
    row["district"] = {"id": None, "name": ""}
    row["workLocation"] = "อ.เมือง จ.ชลบุรี\nรายละเอียดเพิ่มเติม..."
    httpx_mock.add_response(url=_API_URL, json=_envelope([row]))
    jobs = JobThaiScraper("any", job_type_ids=("17",)).fetch()
    assert jobs[0].location == "อ.เมือง จ.ชลบุรี"


def test_location_none_when_all_empty(httpx_mock) -> None:
    """No province, no district, no workLocation → ``location`` is
    None (not an empty string), matching the canonical schema."""
    row = _row()
    row["province"] = {"id": None, "name": ""}
    row["district"] = {"id": None, "name": ""}
    row["workLocation"] = ""
    httpx_mock.add_response(url=_API_URL, json=_envelope([row]))
    jobs = JobThaiScraper("any", job_type_ids=("17",)).fetch()
    assert jobs[0].location is None


# --- field handling ---------------------------------------------------------


def test_skips_rows_missing_id_or_title(httpx_mock) -> None:
    """Defensive: drop malformed rows rather than emit half-built Job
    instances."""
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([
            _row(job_id=1, job_title="Good"),
            {**_row(job_id=2, job_title=""), },  # blank title
            {**_row(job_title="No id"), "id": None},
        ]),
    )
    jobs = JobThaiScraper("any", job_type_ids=("4",)).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_urgent_flag_lifts_to_raw(httpx_mock) -> None:
    """``urgent.id`` > 0 → ``raw.is_urgent`` = True. Zero → omitted."""
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([_row(job_id=1, urgent_id=2), _row(job_id=2, urgent_id=0)]),
    )
    jobs = JobThaiScraper("any", job_type_ids=("4",)).fetch()
    raw_by_id = {j.ats_id: (j.raw or {}) for j in jobs}
    assert raw_by_id["1"].get("is_urgent") is True
    assert "is_urgent" not in raw_by_id["2"]


def test_tags_lift_to_raw(httpx_mock) -> None:
    """Non-empty tags array → ``raw.tags``. Empty array stays out of raw."""
    httpx_mock.add_response(
        url=_API_URL,
        json=_envelope([
            _row(job_id=1, tags=["สัมภาษณ์งานออนไลน์", "Online Interview"]),
            _row(job_id=2, tags=[]),
        ]),
    )
    jobs = JobThaiScraper("any", job_type_ids=("4",)).fetch()
    raw_by_id = {j.ats_id: (j.raw or {}) for j in jobs}
    assert raw_by_id["1"]["tags"] == ["สัมภาษณ์งานออนไลน์", "Online Interview"]
    assert "tags" not in raw_by_id["2"]


# --- error handling ---------------------------------------------------------


def test_graphql_errors_raise(httpx_mock) -> None:
    """Schema drift → GraphQL ``errors`` array → fatal. Don't silently
    emit []."""
    httpx_mock.add_response(
        url=_API_URL,
        json={"errors": [{"message": "Cannot query field 'xyz'"}]},
    )
    with pytest.raises(ScraperError, match="GraphQL errors"):
        JobThaiScraper("any", job_type_ids=("4",)).fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    """Server errors should surface, not silently emit []."""
    httpx_mock.add_response(url=_API_URL, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobThaiScraper("any", job_type_ids=("4",)).fetch()

