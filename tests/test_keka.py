from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import KekaScraper
from ats_scrapers.scrapers.base import ScraperRegistry

PORTAL_URL = "https://100.keka.com/careers"
IDENTIFIER = "7e2f830e-7500-440f-992f-5013e438f8b4"
JOBS_URL = (
    "https://100.keka.com/careers/api/embedjobs/"
    f"default/active/{IDENTIFIER}"
)
COMPANY_URL = (
    "https://100.keka.com/careers/api/organization/"
    "default/careerportalinfo"
)


def _bootstrap(identifier: str = IDENTIFIER) -> str:
    return (
        "<html><body>"
        f'<img src="/ats/documents/{identifier}/careerportal/logo.png">'
        "</body></html>"
    )


def _job(job_id: int = 153027) -> dict[str, object]:
    return {
        "id": job_id,
        "title": "Manager, Marketing & Communication",
        "description": "<h2>About us</h2><p>Build social impact.</p>",
        "departmentIdentifier": "department-1",
        "departmentName": "Marketing",
        "jobLocations": [
            {
                "id": 6459,
                "name": "Chembur East (HO)",
                "city": "Mumbai",
                "state": "MH",
                "countryCode": "IN",
                "countryName": "India",
            }
        ],
        "jobType": 2,
        "experience": "7 - 10 years",
        "jobNumber": "BF220",
        "salaryRange": {
            "minimum": 300000.0,
            "maximum": 360000.0,
            "currency": "INR",
            "salaryPeriod": 4,
            "cultureInfo": "en-IN",
        },
        "salaryRangeFormat": "INR 3,00,000.00 - 3,60,000.00",
        "publishedOn": "2026-07-29T11:37:25.473Z",
        "publishedSinceDays": 1,
        "skillNames": ["Communication", "Marketing"],
    }


def _mock_portal(httpx_mock, jobs: object) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(url=JOBS_URL, json=jobs)
    httpx_mock.add_response(
        url=COMPANY_URL,
        json={"shortName": "Bright Future"},
    )


def test_registry_resolves_keka() -> None:
    assert ScraperRegistry.get(ATSType.KEKA) is KekaScraper


def test_fetches_structured_public_jobs(httpx_mock) -> None:
    _mock_portal(httpx_mock, [_job()])

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.ats_type is ATSType.KEKA
    assert job.ats_id == "100.keka.com:default:153027"
    assert job.title == "Manager, Marketing & Communication"
    assert job.company == "Bright Future"
    assert str(job.url) == "https://100.keka.com/careers/jobdetails/153027"
    assert str(job.apply_url) == str(job.url)
    assert job.location == "Mumbai, MH, India"
    assert job.country_iso == "IN"
    assert job.region == "Asia"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full Time"
    assert job.experience == 7
    assert job.department == "Marketing"
    assert job.requisition_id == "BF220"
    assert job.description == "About us\nBuild social impact."
    assert job.salary_min == 300000.0
    assert job.salary_max == 360000.0
    assert job.salary_currency == "INR"
    assert job.salary_period == "YEAR"
    assert job.salary_summary == "INR 3,00,000.00 - 3,60,000.00"
    assert job.posted_at == datetime(
        2026, 7, 29, 11, 37, 25, 473000, tzinfo=UTC
    )
    assert job.raw == {
        "portal": "default",
        "portal_identifier": IDENTIFIER,
        "department_identifier": "department-1",
        "experience": "7 - 10 years",
        "published_since_days": 1,
        "salary_period": "Annual",
        "skills": ["Communication", "Marketing"],
    }


def test_explicit_company_name_wins_over_api(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(url=JOBS_URL, json=[_job()])

    job = KekaScraper(PORTAL_URL, company_name="Bright Future India").fetch()[0]

    assert job.company == "Bright Future India"


def test_company_metadata_failure_uses_hostname_fallback(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(url=JOBS_URL, json=[_job()])
    httpx_mock.add_response(
        url=COMPANY_URL,
        status_code=500,
        is_reusable=True,
    )

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.company == "100"


def test_listing_only_mode_omits_description(httpx_mock) -> None:
    _mock_portal(httpx_mock, [_job()])

    job = KekaScraper(PORTAL_URL, include_descriptions=False).fetch()[0]

    assert job.description is None
    assert job.ats_id == "100.keka.com:default:153027"


def test_custom_portal_uses_portal_specific_endpoints(httpx_mock) -> None:
    portal_url = "https://universaled.keka.com/careers/ebenezerschool"
    identifier = "da170ad4-577d-47c3-bbf1-31f323c39586"
    httpx_mock.add_response(url=portal_url, text=_bootstrap(identifier))
    httpx_mock.add_response(
        url=(
            "https://universaled.keka.com/careers/api/embedjobs/"
            f"ebenezerschool/active/{identifier}"
        ),
        json=[_job(123)],
    )
    httpx_mock.add_response(
        url=(
            "https://universaled.keka.com/careers/api/organization/"
            "ebenezerschool/careerportalinfo"
        ),
        json={"name": "Ebenezer International School"},
    )

    job = KekaScraper(portal_url).fetch()[0]

    assert job.company == "Ebenezer International School"
    assert job.ats_id == "universaled.keka.com:ebenezerschool:123"
    assert (
        str(job.url)
        == "https://universaled.keka.com/careers/ebenezerschool/jobdetails/123"
    )


def test_country_name_corrects_legacy_keka_country_code(httpx_mock) -> None:
    job_payload = _job()
    job_payload["jobLocations"] = [
        {
            "city": "Libreville",
            "countryCode": "GB",
            "countryName": "Gabon",
        }
    ]
    _mock_portal(httpx_mock, [job_payload])

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.country_iso == "GA"
    assert job.region == "Africa"


def test_valid_country_code_survives_without_region_mapping(httpx_mock) -> None:
    job_payload = _job()
    job_payload["jobLocations"] = [
        {
            "city": "Paris",
            "countryCode": "FR",
            "countryName": "République française",
        }
    ]
    _mock_portal(httpx_mock, [job_payload])

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.country_iso == "FR"
    assert job.region is None


def test_biweekly_salary_keeps_raw_period_without_invalid_enum(
    httpx_mock,
) -> None:
    job_payload = _job()
    job_payload["salaryRange"] = {
        "minimum": 1000,
        "maximum": 1500,
        "currency": "USD",
        "salaryPeriod": 2,
    }
    _mock_portal(httpx_mock, [job_payload])

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.salary_period is None
    assert job.raw is not None
    assert job.raw["salary_period"] == "Bi Weekly"


def test_unavailable_salary_does_not_export_zero_range(httpx_mock) -> None:
    job_payload = _job()
    job_payload["salaryRange"] = {
        "minimum": 0,
        "maximum": 0,
        "currency": "INR",
        "salaryPeriod": 0,
    }
    job_payload["salaryRangeFormat"] = "Not Available"
    _mock_portal(httpx_mock, [job_payload])

    job = KekaScraper(PORTAL_URL).fetch()[0]

    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_period is None
    assert job.salary_summary is None


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    _mock_portal(httpx_mock, [_job(), _job()])

    with pytest.raises(ScraperError, match="duplicate job ID"):
        KekaScraper(PORTAL_URL).fetch()


def test_malformed_jobs_payload_fails_closed(httpx_mock) -> None:
    _mock_portal(httpx_mock, {"jobs": []})

    with pytest.raises(ScraperError, match="was not a list"):
        KekaScraper(PORTAL_URL).fetch()


def test_missing_portal_identifier_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text="<html>maintenance</html>")

    with pytest.raises(ScraperError, match="omitted its portal identifier"):
        KekaScraper(PORTAL_URL).fetch()


def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        KekaScraper(PORTAL_URL).fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "100.keka.com",
        "https://keka.com/careers",
        "https://bad_slug.keka.com/careers",
        "https://100.keka.com/careers/default/extra",
        "https://100.keka.com/careers?redirect=https://evil.example",
        "https://evil.example/careers",
        "https://100.keka.com.evil.example/careers",
    ],
)
def test_rejects_untrusted_portal_urls(slug: str) -> None:
    with pytest.raises(ScraperError, match="KekaScraper"):
        KekaScraper(slug)
