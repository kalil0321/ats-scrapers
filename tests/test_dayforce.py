"""Tests for the anonymous Dayforce job-feed scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.dayforce import (
    DayforceScraper,
    _normalize_tenant_board,
)

API_URL = (
    "https://www.dayforcehcm.com/api/mayfair/V1/JobFeeds"
    "?includeActivePostingOnly=true&internalJobBoardCode=CANDIDATEPORTAL"
)

FIXTURE = {
    "Title": "Accounting & Payroll Manager",
    "Description": "<p>Lead finance operations.</p><p>Support patients.</p>",
    "ClientSiteName": "Client Careers Site",
    "ClientSiteXRefCode": "CANDIDATEPORTAL",
    "CompanyName": "Mayfair Diagnostics",
    "ParentCompanyName": "Mayfair Diagnostics",
    "JobDetailsUrl": (
        "https://jobs.dayforcehcm.com/en-CA/mayfair/"
        "CANDIDATEPORTAL/jobs/3612"
    ),
    "ApplyUrl": (
        "https://jobs.dayforcehcm.com/en-CA/mayfair/"
        "CANDIDATEPORTAL/jobs/3612/apply"
    ),
    "AddressLine1": "132, 6707 Elbow Drive SW",
    "City": "Calgary",
    "State": "AB",
    "Country": "CAN",
    "PostalCode": "T2V0E3",
    "JobFamily": "Finance",
    "JobFunction": "Accounting",
    "EmploymentIndicator": "Full Time",
    "DatePosted": "2026-05-19T02:00:00",
    "LastUpdated": "2026-06-15T11:05:48.243",
    "ReferenceNumber": 3612,
    "CultureCode": "en-CA",
    "ParentRequisitionCode": 854,
    "JobType": 0,
    "TravelRequired": 0,
    "IsVirtualLocation": False,
}


def test_fetches_and_maps_public_feed(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[FIXTURE])

    jobs = DayforceScraper(
        "mayfair/CANDIDATEPORTAL",
        company_name="Mayfair Diagnostics",
    ).fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.DAYFORCE
    assert job.ats_id == "mayfair:CANDIDATEPORTAL:3612"
    assert job.global_id == "dayforce:mayfair:CANDIDATEPORTAL:3612"
    assert job.title == "Accounting & Payroll Manager"
    assert job.company == "Mayfair Diagnostics"
    assert job.requisition_id == "3612"
    assert job.location == "132, 6707 Elbow Drive SW, Calgary, AB, CAN"
    assert job.country_iso == "CA"
    assert job.region == "North America"
    assert job.is_remote is False
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full Time"
    assert job.department == "Finance"
    assert job.team == "Accounting"
    assert job.language == "en"
    assert job.description == (
        "<p>Lead finance operations.</p><p>Support patients.</p>"
    )
    assert job.posted_at is not None
    assert str(job.url).endswith("/mayfair/CANDIDATEPORTAL/jobs/3612")
    assert str(job.apply_url).endswith(
        "/mayfair/CANDIDATEPORTAL/jobs/3612/apply"
    )
    assert job.raw is not None
    assert job.raw["ParentRequisitionCode"] == 854


def test_seed_company_name_overrides_feed_name(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[FIXTURE])

    jobs = DayforceScraper(
        "mayfair/CANDIDATEPORTAL",
        company_name="Canonical Mayfair",
    ).fetch()

    assert jobs[0].company == "Canonical Mayfair"


def test_include_descriptions_false_omits_inline_description(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[FIXTURE])

    jobs = DayforceScraper(
        "mayfair/CANDIDATEPORTAL",
        include_descriptions=False,
    ).fetch()

    assert jobs[0].description is None


def test_explicit_empty_feed_is_valid(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[])

    assert DayforceScraper("mayfair/CANDIDATEPORTAL").fetch() == []


@pytest.mark.parametrize("payload", [{}, {"jobs": []}, "maintenance"])
def test_non_list_feed_fails_closed(httpx_mock, payload: object) -> None:
    httpx_mock.add_response(url=API_URL, json=payload)

    with pytest.raises(ScraperError, match="non-list feed"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


SITE = "https://jobs.dayforcehcm.com"
SEARCH_URL = f"{SITE}/api/geo/mayfair/jobposting/search"
IP_RESTRICTED = {
    "processResults": [
        {
            "code": "SYS_IP_RESTRICTED",
            "level": "ERROR",
            "message": "IP address is not authorized to access the Dayforce Services API.",
        }
    ]
}


def _posting(posting_id: int, *locations: dict[str, object]) -> dict[str, object]:
    return {
        "clientNamespace": "mayfair",
        "jobPostingId": posting_id,
        "jobTitle": f"Technologist {posting_id}",
        "jobDescription": "Read scans &amp; reports.",
        "hasVirtualLocation": False,
        "postingStartTimestampUTC": "2026-11-24T08:00:00+00:00",
        "postingLocations": list(locations),
    }


CALGARY = {"cityName": "Calgary", "stateCode": "AB", "isoCountryCode": "CA"}
EDMONTON = {"cityName": "Edmonton", "stateCode": "AB", "isoCountryCode": "CA"}


def test_ip_restricted_feed_is_read_from_the_career_site(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, status_code=403, json=IP_RESTRICTED)
    httpx_mock.add_response(url=f"{SITE}/api/auth/csrf", json={"csrfToken": "tok"})
    first = [_posting(3612, CALGARY, EDMONTON)] + [
        _posting(4000 + index, CALGARY) for index in range(24)
    ]
    for offset, postings in ((0, first), (25, [_posting(5000)])):
        httpx_mock.add_response(
            url=SEARCH_URL,
            method="POST",
            match_headers={"X-CSRF-TOKEN": "tok"},
            match_json={
                "clientNamespace": "mayfair",
                "jobBoardCode": "CANDIDATEPORTAL",
                "cultureCode": "en-US",
                "paginationStart": offset,
            },
            json={"jobPostings": postings, "maxCount": 26, "offset": offset},
        )

    jobs = DayforceScraper("mayfair/CANDIDATEPORTAL", company_name="Mayfair").fetch()

    assert len(jobs) == 26
    job = next(job for job in jobs if job.requisition_id == "3612")
    assert job.ats_id == "mayfair:CANDIDATEPORTAL:3612"
    assert job.title == "Technologist 3612"
    assert job.company == "Mayfair"
    assert str(job.url) == f"{SITE}/en-US/mayfair/CANDIDATEPORTAL/jobs/3612"
    assert job.location == "Calgary, AB, CA"
    assert job.country_iso == "CA"
    assert job.is_remote is False
    assert job.description == "Read scans & reports."
    assert job.posted_at is not None and job.posted_at.isoformat() == "2026-11-24T08:00:00+00:00"
    assert job.raw == {"CultureCode": "en-US", "AllLocations": ["Calgary, AB, CA", "Edmonton, AB, CA"]}
    assert next(job for job in jobs if job.requisition_id == "5000").location is None


def _ip_restricted_site(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, status_code=403, json=IP_RESTRICTED)
    httpx_mock.add_response(url=f"{SITE}/api/auth/csrf", json={"csrfToken": "tok"})


def test_career_site_search_that_stops_short_fails(httpx_mock) -> None:
    _ip_restricted_site(httpx_mock)
    first = [_posting(4000 + index, CALGARY) for index in range(25)]
    for offset, postings in ((0, first), (25, [])):
        httpx_mock.add_response(
            url=SEARCH_URL,
            method="POST",
            match_json={
                "clientNamespace": "mayfair",
                "jobBoardCode": "CANDIDATEPORTAL",
                "cultureCode": "en-US",
                "paginationStart": offset,
            },
            json={"jobPostings": postings, "maxCount": 50},
        )

    with pytest.raises(ScraperError, match="stopped at 25 of 50"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


@pytest.mark.parametrize("count", [None, "26", 26.0, True, -1])
def test_career_site_search_without_a_count_fails(httpx_mock, count: object) -> None:
    _ip_restricted_site(httpx_mock)
    page: dict[str, object] = {"jobPostings": [_posting(3612, CALGARY)]}
    if count is not None:
        page["maxCount"] = count
    httpx_mock.add_response(url=SEARCH_URL, method="POST", json=page)

    with pytest.raises(ScraperError, match="no posting count"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


def test_career_site_search_of_an_empty_board(httpx_mock) -> None:
    _ip_restricted_site(httpx_mock)
    httpx_mock.add_response(
        url=SEARCH_URL, method="POST", json={"jobPostings": [], "maxCount": 0}
    )

    assert DayforceScraper("mayfair/CANDIDATEPORTAL").fetch() == []


def test_other_403_still_reports_the_block(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, status_code=403, text="Forbidden")

    with pytest.raises(ScraperError, match="returned 403"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


def test_non_object_row_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=API_URL, json=[FIXTURE, "bad row"])

    with pytest.raises(ScraperError, match="row 1 was not an object"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


@pytest.mark.parametrize("field", ["Title", "ReferenceNumber"])
def test_required_fields_fail_closed(httpx_mock, field: str) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=[{key: value for key, value in FIXTURE.items() if key != field}],
    )

    with pytest.raises(ScraperError, match=f"omitted {field}"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


def test_duplicate_references_fail_closed(httpx_mock) -> None:
    duplicate = {**FIXTURE, "Title": "Another title"}
    httpx_mock.add_response(url=API_URL, json=[FIXTURE, duplicate])

    with pytest.raises(ScraperError, match="duplicate job id"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


def test_duplicate_feed_employers_fail_closed_with_seed_override(
    httpx_mock,
) -> None:
    duplicate = {
        **FIXTURE,
        "CompanyName": "Another Employer",
        "ParentCompanyName": "Another Employer",
    }
    httpx_mock.add_response(url=API_URL, json=[FIXTURE, duplicate])

    with pytest.raises(ScraperError, match="duplicate job id"):
        DayforceScraper(
            "mayfair/CANDIDATEPORTAL",
            company_name="Canonical Employer",
        ).fetch()


def test_equivalent_duplicate_references_are_collapsed(httpx_mock) -> None:
    duplicate = {**FIXTURE, "Country": "CA"}
    httpx_mock.add_response(url=API_URL, json=[FIXTURE, duplicate])

    jobs = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()

    assert len(jobs) == 1
    assert jobs[0].country_iso == "CA"
    assert jobs[0].region == "North America"


def test_multilingual_duplicate_prefers_english(httpx_mock) -> None:
    french = {
        **FIXTURE,
        "Title": "Gestionnaire de la comptabilité et de la paie",
        "CultureCode": "fr-CA",
        "JobDetailsUrl": (
            "https://jobs.dayforcehcm.com/fr-CA/mayfair/"
            "CANDIDATEPORTAL/jobs/3612"
        ),
        "ApplyUrl": (
            "https://jobs.dayforcehcm.com/fr-CA/mayfair/"
            "CANDIDATEPORTAL/jobs/3612/apply"
        ),
    }
    httpx_mock.add_response(url=API_URL, json=[french, FIXTURE])

    jobs = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()

    assert len(jobs) == 1
    assert jobs[0].title == FIXTURE["Title"]
    assert jobs[0].language == "en"
    assert jobs[0].raw is not None
    assert jobs[0].raw["AvailableCultures"] == ["en-CA", "fr-CA"]


def test_multilingual_duplicate_keeps_available_description(httpx_mock) -> None:
    english = {**FIXTURE, "Description": ""}
    french = {
        **FIXTURE,
        "Title": "Gestionnaire de la comptabilité et de la paie",
        "CultureCode": "fr-CA",
        "JobDetailsUrl": (
            "https://jobs.dayforcehcm.com/fr-CA/mayfair/"
            "CANDIDATEPORTAL/jobs/3612"
        ),
        "ApplyUrl": (
            "https://jobs.dayforcehcm.com/fr-CA/mayfair/"
            "CANDIDATEPORTAL/jobs/3612/apply"
        ),
    }
    httpx_mock.add_response(url=API_URL, json=[english, french])

    jobs = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()

    assert len(jobs) == 1
    assert jobs[0].description == FIXTURE["Description"]
    assert jobs[0].language == "fr"


def test_multi_location_duplicate_preserves_all_locations(httpx_mock) -> None:
    edmonton = {
        **FIXTURE,
        "AddressLine1": "100 Main Street",
        "City": "Edmonton",
        "State": "AB",
        "PostalCode": "T5J0N3",
    }
    httpx_mock.add_response(url=API_URL, json=[FIXTURE, edmonton])

    jobs = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()

    assert len(jobs) == 1
    assert jobs[0].raw is not None
    assert jobs[0].raw["AllLocations"] == [
        "132, 6707 Elbow Drive SW, Calgary, AB, CAN",
        "100 Main Street, Edmonton, AB, CAN",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/en-CA/mayfair/CANDIDATEPORTAL/jobs/3612",
        "https://jobs.dayforcehcm.com/en-CA/other/CANDIDATEPORTAL/jobs/3612",
        "https://jobs.dayforcehcm.com/en-CA/mayfair/OTHER/jobs/3612",
        "https://jobs.dayforcehcm.com/en-CA/mayfair/CANDIDATEPORTAL/jobs/999",
        "https://jobs.dayforcehcm.com:bad/en-CA/mayfair/CANDIDATEPORTAL/jobs/3612",
    ],
)
def test_unsafe_detail_url_fails_closed(httpx_mock, url: str) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=[{**FIXTURE, "JobDetailsUrl": url}],
    )

    with pytest.raises(ScraperError, match="unsafe job URL"):
        DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()


def test_missing_detail_url_uses_canonical_fallback(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=[{**FIXTURE, "JobDetailsUrl": ""}],
    )

    job = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()[0]

    assert str(job.url) == (
        "https://jobs.dayforcehcm.com/en-CA/mayfair/"
        "CANDIDATEPORTAL/jobs/3612"
    )


def test_description_preserves_html_for_pipeline_normalization(httpx_mock) -> None:
    description = (
        "<h2>Responsibilities</h2><ul><li>Build systems</li></ul>"
        '<p><a href="https://example.com/team">Meet the team</a></p>'
    )
    httpx_mock.add_response(
        url=API_URL,
        json=[{**FIXTURE, "Description": description}],
    )

    job = DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()[0]

    assert job.description == description


@pytest.mark.parametrize(
    "url",
    [
        "javascript:void(0)",
        "https://evil.example/apply",
        "https://jobs.dayforcehcm.com/en-CA/mayfair/OTHER/jobs/3612/apply",
        "https://jobs.dayforcehcm.com/en-CA/mayfair/CANDIDATEPORTAL/jobs/999/apply",
    ],
)
def test_invalid_apply_url_is_ignored(httpx_mock, url: str) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=[{**FIXTURE, "ApplyUrl": url}],
    )

    assert DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()[0].apply_url is None


def test_virtual_location_sets_remote(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_URL,
        json=[{**FIXTURE, "IsVirtualLocation": True}],
    )

    assert DayforceScraper("mayfair/CANDIDATEPORTAL").fetch()[0].is_remote is True


def test_scraper_is_registered() -> None:
    assert ScraperRegistry.get(ATSType.DAYFORCE) is DayforceScraper


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mayfair/CANDIDATEPORTAL", ("mayfair", "CANDIDATEPORTAL")),
        ("fi/CANDIDATEPORTAL", ("fi", "CANDIDATEPORTAL")),
        ("gm/CANDIDATEPORTAL", ("gm", "CANDIDATEPORTAL")),
        (
            "https://jobs.dayforcehcm.com/en-CA/mayfair/"
            "CANDIDATEPORTAL/jobs/3612",
            ("mayfair", "CANDIDATEPORTAL"),
        ),
        (
            "https://jobs.dayforcehcm.com/mayfair/CANDIDATEPORTAL",
            ("mayfair", "CANDIDATEPORTAL"),
        ),
        (
            "https://jobs.dayforcehcm.com/fi/CANDIDATEPORTAL/jobs/123",
            ("fi", "CANDIDATEPORTAL"),
        ),
        (
            "https://jobs.dayforcehcm.com/gm/CANDIDATEPORTAL/jobs",
            ("gm", "CANDIDATEPORTAL"),
        ),
    ],
)
def test_normalizes_tenant_board(
    value: str,
    expected: tuple[str, str],
) -> None:
    assert _normalize_tenant_board(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "mayfair",
        "../mayfair/CANDIDATEPORTAL",
        "mayfair/CANDIDATEPORTAL/other",
        "http://jobs.dayforcehcm.com/mayfair/CANDIDATEPORTAL",
        "https://evil.example/mayfair/CANDIDATEPORTAL",
        "https://jobs.dayforcehcm.com/mayfair/CANDIDATEPORTAL?x=1",
    ],
)
def test_rejects_invalid_tenant_board(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_tenant_board(value)


def test_job_function_and_explicit_required_education() -> None:
    job = DayforceScraper("example/CANDIDATEPORTAL")._parse_job({
        "Title": "Team Lead", "ReferenceNumber": 1, "JobFunction": " Supervisory Management ", "Education": "Bachelor’s Degree",
    })
    assert job.job_category == "Supervisory Management"
    assert [q.model_dump() for q in job.education_requirements] == [
        {"label": "Bachelor’s Degree", "requirement": "required"},
    ]


@pytest.mark.parametrize("value", ["Unspecified", "Equivalent Experience", "Expérience équivalente", 0])
def test_job_family_and_experience_are_not_degree_requirements(value: object) -> None:
    job = DayforceScraper("example/CANDIDATEPORTAL")._parse_job({
        "Title": "Team Lead", "ReferenceNumber": 1, "Education": value, "JobFunction": None, "JobFamily": "Retail",
    })
    assert job.department == "Retail"
    assert job.job_category is None and job.education_requirements is None
