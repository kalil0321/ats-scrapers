"""Tests for the MyCareersFuture (Singapore) scraper.

MyCareersFuture is a single-source public-sector job board (~87k live
listings, no auth, no captcha). These tests pin the parse contract
against a sanitized inline fixture — *no live HTTP*. The fixture mirrors
the shape returned by ``GET https://api.mycareersfuture.gov.sg/v2/jobs``
on 2026-05; field names match the live payload verbatim so parser
breakage is caught at PR time.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import MyCareersFutureScraper

_API_RE = re.compile(r"^https://api\.mycareersfuture\.gov\.sg/v2/jobs")


def _job_record(**overrides: Any) -> dict[str, Any]:
    """Minimal valid MCF job payload, modeled on the live API response.

    Sanitized: real uuids are replaced with shorter test ids; long
    description HTML is trimmed; skill/category lists are abbreviated.
    Everything that the parser actually reads is structurally identical
    to production.
    """
    base: dict[str, Any] = {
        "uuid": "test-uuid-0000000000000000",
        "sourceCode": "Employer Portal",
        "title": "Senior Software Engineer",
        "description": (
            "<ol><li>Build &amp; ship distributed services.</li>"
            "<li>Work with a cross-functional team.</li></ol>"
        ),
        "minimumYearsExperience": 5,
        "skills": [
            {"skill": "Python", "uuid": "s1", "isKeySkill": True},
            {"skill": "Kubernetes", "uuid": "s2", "isKeySkill": False},
        ],
        "schemes": [],
        "flexibleWorkArrangements": [
            {"id": 1, "flexibleWorkArrangement": "Hybrid"},
        ],
        "ssocCode": "25121",
        "occupationId": "OCC000001",
        "ssocVersion": "2020v3",
        "categories": [
            {"id": 11, "category": "Information Technology"},
        ],
        "employmentTypes": [
            {"id": 7, "employmentType": "Permanent"},
        ],
        "positionLevels": [
            {"id": 3, "position": "Senior Executive"},
        ],
        "status": {"id": 102, "jobStatus": "Open"},
        "postedCompany": {
            "uen": "199912345A",
            "name": "ACME PTE. LTD.",
            "_links": {},
        },
        "hiringCompany": None,
        "address": {
            "block": "1",
            "street": "MARINA BOULEVARD",
            "postalCode": "018989",
            "isOverseas": False,
            "districts": [
                {
                    "id": 1,
                    "location": "D01 Boat Quay, Marina, Raffles Place",
                    "region": "Central",
                }
            ],
            "lat": 1.2833,
            "lng": 103.8519,
        },
        "metadata": {
            "jobPostId": "MCF-2026-0000001",
            "createdAt": "2026-05-10T08:30:00.000Z",
            "expiryDate": "2026-06-10",
            "originalPostingDate": "2026-05-10",
            "jobDetailsUrl": (
                "https://www.mycareersfuture.gov.sg/job/information-technology/"
                "senior-software-engineer-acme-test-uuid-0000000000000000"
            ),
        },
        "salary": {
            "maximum": 12000,
            "minimum": 8000,
            "type": {"id": 4, "salaryType": "Monthly"},
        },
        "_links": {
            "self": {
                "href": "https://api.mycareersfuture.gov.sg/v2/jobs/test-uuid-0000000000000000"
            }
        },
    }
    base.update(overrides)
    return base


# --- _parse_job contract -----------------------------------------------------


def test_parse_job_minimal_fields() -> None:
    scraper = MyCareersFutureScraper("sg")
    job = scraper._parse_job(_job_record())
    assert job is not None
    assert job.ats_type is ATSType.MYCAREERSFUTURE
    assert job.ats_id == "test-uuid-0000000000000000"
    assert job.title == "Senior Software Engineer"
    assert job.company == "ACME PTE. LTD."
    assert job.global_id == "mycareersfuture:test-uuid-0000000000000000"


def test_parse_job_country_iso_is_singapore() -> None:
    """All MCF postings are published on the Singapore board — pin SG."""
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.country_iso == "SG"
    assert job.region == "Asia"
    assert job.language == "en"


def test_parse_job_url_prefers_metadata_pretty_url() -> None:
    """``metadata.jobDetailsUrl`` is a human-readable pretty URL — prefer
    it over the bare-uuid fallback when the API gives us one."""
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert str(job.url).startswith(
        "https://www.mycareersfuture.gov.sg/job/information-technology/"
    )


def test_parse_job_url_falls_back_to_uuid() -> None:
    """If ``metadata.jobDetailsUrl`` is missing, build the canonical bare
    ``/job/{uuid}`` URL — verified to resolve on the live site."""
    record = _job_record()
    record["metadata"].pop("jobDetailsUrl")
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert (
        str(job.url)
        == "https://www.mycareersfuture.gov.sg/job/test-uuid-0000000000000000"
    )


def test_parse_job_hiring_company_takes_precedence_over_posted() -> None:
    """When ``hiringCompany`` is set it's the actual employer; fall back
    to ``postedCompany`` (often a recruitment agency) otherwise."""
    record = _job_record(
        hiringCompany={"name": "Hiring Co Ltd", "uen": "X"},
    )
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert job.company == "Hiring Co Ltd"


def test_parse_job_description_html_stripped_and_entities_decoded() -> None:
    """HTML tags are stripped, entities (``&amp;``) are decoded, and
    whitespace is collapsed before truncation."""
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.description is not None
    assert "<ol>" not in job.description
    assert "<li>" not in job.description
    assert "&amp;" not in job.description
    assert "Build & ship" in job.description


def test_parse_job_salary_monthly_maps_to_month_period() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.salary_currency == "SGD"
    assert job.salary_period == "MONTH"
    assert job.salary_min == 8000
    assert job.salary_max == 12000
    assert job.salary_summary is not None
    assert "SGD" in job.salary_summary
    assert "/month" in job.salary_summary


def test_parse_job_salary_absent_leaves_currency_none() -> None:
    """No salary on the record → no salary fields populated. Pydantic
    must not require ``salary_currency`` when min/max are both null."""
    record = _job_record(salary=None)
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert job.salary_currency is None
    assert job.salary_min is None
    assert job.salary_max is None


def test_parse_job_employment_type_maps_permanent_to_full_time() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Permanent"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Permanent", "FULL_TIME"),
        ("Full Time", "FULL_TIME"),
        ("Part Time", "PART_TIME"),
        ("Contract", "CONTRACT"),
        ("Freelance", "CONTRACT"),
        ("Internship", "INTERN"),
        ("Temporary", "TEMPORARY"),
        ("Flexi-work", "TEMPORARY"),
    ],
)
def test_employment_type_mapping(label: str, expected: str) -> None:
    record = _job_record(
        employmentTypes=[{"id": 1, "employmentType": label}],
    )
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert job.employment_type == expected


def test_parse_job_location_uses_district_label() -> None:
    """Singapore district labels are the closest analog to neighbourhood,
    so we surface them as ``location`` rather than a free-form street."""
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.location is not None
    assert "Singapore" in job.location
    assert "Boat Quay" in job.location


def test_parse_job_lat_lng_passed_through() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.lat == pytest.approx(1.2833)
    assert job.lon == pytest.approx(103.8519)


def test_parse_job_overseas_uses_overseas_country() -> None:
    """Overseas-tagged postings still publish on the SG board; the
    ``location`` field should surface the foreign country name."""
    record = _job_record(
        address={
            "isOverseas": True,
            "overseasCountry": "Malaysia",
            "districts": [],
        },
    )
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert job.location == "Malaysia"
    assert job.country_iso is None
    assert job.region is None


def test_parse_job_requisition_id_from_job_post_id() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.requisition_id == "MCF-2026-0000001"


def test_parse_job_posted_at_parsed_from_created_at() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.posted_at is not None
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 5


def test_parse_job_experience_passes_through_when_int() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert job.experience == 5


def test_parse_job_experience_none_when_null() -> None:
    record = _job_record(minimumYearsExperience=None)
    job = MyCareersFutureScraper("sg")._parse_job(record)
    assert job is not None
    assert job.experience is None


def test_parse_job_raw_contains_skills_and_taxonomy() -> None:
    job = MyCareersFutureScraper("sg")._parse_job(_job_record())
    assert job is not None
    assert isinstance(job.raw, dict)
    assert "skills" in job.raw
    assert "Python" in job.raw["skills"]
    assert job.raw.get("ssocCode") == "25121"
    assert job.raw.get("occupationId") == "OCC000001"


def test_parse_job_returns_none_on_missing_uuid() -> None:
    """No uuid → no global_id → skip the row defensively."""
    record = _job_record(uuid="")
    assert MyCareersFutureScraper("sg")._parse_job(record) is None


def test_parse_job_returns_none_on_missing_title() -> None:
    record = _job_record(title="")
    assert MyCareersFutureScraper("sg")._parse_job(record) is None


def test_parse_job_returns_none_on_missing_company() -> None:
    """Both ``hiringCompany`` and ``postedCompany.name`` empty → drop."""
    record = _job_record(
        hiringCompany=None,
        postedCompany={"uen": "X"},
    )
    assert MyCareersFutureScraper("sg")._parse_job(record) is None


# --- registry + ATSType ------------------------------------------------------


def test_scraper_registered_in_registry() -> None:
    """The ``@ScraperRegistry.register`` decorator must wire the class
    to ``ATSType.MYCAREERSFUTURE`` so ``get_scraper`` finds it."""
    from ats_scrapers.scrapers.base import ScraperRegistry
    assert ScraperRegistry.get(ATSType.MYCAREERSFUTURE) is MyCareersFutureScraper


def test_scraper_registered_by_string_lookup() -> None:
    """``ScraperRegistry.get("mycareersfuture")`` is the manifest path —
    must resolve to the scraper class even when called with a raw string."""
    from ats_scrapers.scrapers.base import ScraperRegistry
    assert ScraperRegistry.get("mycareersfuture") is MyCareersFutureScraper


# --- pagination & retries (fixture-driven, no live HTTP) ---------------------


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry delays so failure-path tests run in milliseconds."""
    import ats_scrapers.scrapers.mycareersfuture as mcf
    monkeypatch.setattr(mcf, "MAX_RETRIES", 2)
    monkeypatch.setattr(mcf, "RETRY_BASE_DELAY", 0.0)


def test_fetch_paginates_until_total_reached(httpx_mock, monkeypatch) -> None:
    """``fetch`` walks ``offset`` in ``PAGE_SIZE`` strides until it has
    covered ``total``. Use a small PAGE_SIZE so the test stays compact."""
    import ats_scrapers.scrapers.mycareersfuture as mcf
    monkeypatch.setattr(mcf, "PAGE_SIZE", 2)

    def serve(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(str(request.url)).query)
        offset = int(qs.get("offset", ["0"])[0])
        # 5 jobs total → offsets 0, 2, 4 → 3 pages (last page has 1 row).
        all_records = [
            _job_record(uuid=f"uuid-{i}", title=f"Job {i}") for i in range(5)
        ]
        page = all_records[offset : offset + 2]
        return httpx.Response(200, json={"results": page, "total": 5})

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    jobs = MyCareersFutureScraper("sg").fetch()
    ats_ids = {j.ats_id for j in jobs}
    assert ats_ids == {f"uuid-{i}" for i in range(5)}


def test_fetch_dedups_overlapping_pages(httpx_mock, monkeypatch) -> None:
    """If two pages return the same uuid (rare but possible during a live
    insertion), the second occurrence is dropped — the scraper's
    ``seen`` set is the dedup guard."""
    import ats_scrapers.scrapers.mycareersfuture as mcf
    monkeypatch.setattr(mcf, "PAGE_SIZE", 2)

    def serve(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(str(request.url)).query)
        offset = int(qs.get("offset", ["0"])[0])
        # 3 total, but page 2 (offset=2) repeats uuid-1 — must be deduped.
        if offset == 0:
            page = [_job_record(uuid="uuid-0"), _job_record(uuid="uuid-1")]
        else:
            page = [_job_record(uuid="uuid-1"), _job_record(uuid="uuid-2")]
        return httpx.Response(200, json={"results": page, "total": 3})

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    jobs = MyCareersFutureScraper("sg").fetch()
    assert sorted(j.ats_id for j in jobs) == ["uuid-0", "uuid-1", "uuid-2"]


def test_fetch_retries_on_5xx_then_succeeds(httpx_mock) -> None:
    """A transient 503 must be retried (up to ``MAX_RETRIES``) before the
    scraper gives up. The first attempt fails, the second succeeds."""
    httpx_mock.add_response(url=_API_RE, status_code=503)
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        json={"results": [_job_record()], "total": 1},
    )
    jobs = MyCareersFutureScraper("sg").fetch()
    assert len(jobs) == 1
    assert jobs[0].ats_id == "test-uuid-0000000000000000"


def test_fetch_raises_after_persistent_5xx(httpx_mock) -> None:
    """A persistent 5xx (longer than the retry budget) must surface as a
    ``ScraperError`` — silent ``[]`` would publish an undercount as a
    successful run."""
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        MyCareersFutureScraper("sg").fetch()


def test_fetch_raises_on_non_json_response(httpx_mock) -> None:
    """A 200 OK with a malformed body is a contract break — crash rather
    than soft-fail to ``[]``."""
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        content=b"<html>Maintenance</html>",
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        MyCareersFutureScraper("sg").fetch()


def test_fetch_raises_when_total_is_missing(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json={"results": [_job_record()]})
    with pytest.raises(ScraperError, match="valid total"):
        MyCareersFutureScraper("sg").fetch()


@pytest.mark.parametrize("payload", [[], None, {"total": 1}, {"results": None, "total": 1}])
def test_fetch_rejects_invalid_envelopes(httpx_mock, payload) -> None:
    httpx_mock.add_response(url=_API_RE, json=payload)
    with pytest.raises(ScraperError, match=r"API shape changed|non-JSON"):
        MyCareersFutureScraper("sg").fetch()


def test_parse_iso_normalizes_naive_and_offset_values_to_utc() -> None:
    from ats_scrapers.scrapers.mycareersfuture import _parse_iso

    naive = _parse_iso("2026-05-10")
    offset = _parse_iso("2026-05-10T03:00:00+03:00")
    assert naive is not None and naive.tzinfo is not None
    assert offset is not None and offset.utcoffset().total_seconds() == 0
    assert offset.hour == 0
