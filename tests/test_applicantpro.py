from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ApplicantProScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "kirkhill"
DOMAIN_ID = "19218"
CAREERS_URL = f"https://{TENANT}.applicantpro.com/jobs/"
LISTING_URL = (
    f"https://{TENANT}.applicantpro.com/core/jobs/{DOMAIN_ID}"
    "?getParams=%7B%7D"
)
DETAIL_URL = (
    f"https://{TENANT}.applicantpro.com/core/jobs/{DOMAIN_ID}/4124648/job-details"
)


def _portal() -> str:
    return """
    <html>
      <head><title>Job Listings - Kirkhill Inc. Jobs</title></head>
      <body>
        <job-listings></job-listings>
        <script>
          const app = { componentData: { domainId : 19218 } };
        </script>
      </body>
    </html>
    """


def _job(job_id: int = 4124648) -> dict[str, object]:
    return {
        "id": job_id,
        "title": "Project Engineer",
        "city": "Brea",
        "subdomain": TENANT,
        "iso3": "USA",
        "abbreviation": "CA",
        "classification": "Operations",
        "siteId": 19218,
        "startDateRef": "Jun 22, 2026",
        "endDateRef": "Jun 22, 2031",
        "untilFilled": 1,
        "orgTitle": "Engineering",
        "parentTitle": "Product",
        "domainName": "applicantpro.com",
        "stateName": "California",
        "workplaceType": "Onsite",
        "employmentType": "Full Time",
        "jobCategory": None,
        "customCategory": None,
        "payRate": None,
        "payType": "Salary",
        "payTypeFrame": "per year",
        "payDetails": "DOE",
        "minSalary": "110,000",
        "maxSalary": "140,000",
        "jobLocation": "Brea, CA, USA, 92821",
        "streetAddress": "",
        "chatToApplyEnabled": "0",
        "jobUrl": f"https://{TENANT}.applicantpro.com/jobs/{job_id}",
    }


def _listing(
    jobs: list[dict[str, object]],
    *,
    count: int | None = None,
) -> dict[str, object]:
    return {
        "success": True,
        "message": "This is where the full jobs list will show",
        "data": {
            "jobs": jobs,
            "jobCount": len(jobs) if count is None else count,
            "jobInfoOrder": {"locations": 1, "employmentTypes": 2},
        },
    }


def _detail(job_id: int = 4124648) -> dict[str, object]:
    return {
        "success": True,
        "message": "",
        "data": {
            "id": job_id,
            "title": "Project Engineer",
            "city": "Brea",
            "benefits": "Medical, dental, and vision",
            "siteId": 19218,
            "startDateRef": "22-Jun-2026",
            "endDateRef": "22-Jun-2031",
            "description": "<p>Fallback description.</p>",
            "advertisingDescriptionHtml": (
                "<h2>Summary</h2><p>Lead <strong>aircraft</strong> "
                "product development.</p>"
            ),
            "hideFromIndeed": 0,
            "untilFilled": 1,
        },
    }


def _mock_listing(httpx_mock, jobs: list[dict[str, object]]) -> None:
    httpx_mock.add_response(url=CAREERS_URL, text=_portal())
    httpx_mock.add_response(url=LISTING_URL, json=_listing(jobs))


def test_registry_resolves_applicantpro() -> None:
    assert ScraperRegistry.get(ATSType.APPLICANTPRO) is ApplicantProScraper


def test_fetches_structured_public_jobs(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    httpx_mock.add_response(url=DETAIL_URL, json=_detail())

    job = ApplicantProScraper(TENANT).fetch()[0]

    assert job.ats_type is ATSType.APPLICANTPRO
    assert job.ats_id == "kirkhill:4124648"
    assert job.title == "Project Engineer"
    assert job.company == "Kirkhill Inc."
    assert str(job.url) == "https://kirkhill.applicantpro.com/jobs/4124648"
    assert str(job.apply_url) == str(job.url)
    assert job.location == "Brea, CA, USA, 92821"
    assert job.country_iso == "US"
    assert job.region == "North America"
    assert job.is_remote is False
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEAR"
    assert job.salary_min == 110_000
    assert job.salary_max == 140_000
    assert job.salary_summary == "110,000 - 140,000 per year"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full Time"
    assert job.department == "Engineering"
    assert job.team == "Product"
    assert job.requisition_id == "4124648"
    assert job.description == (
        "<h2>Summary</h2><p>Lead <strong>aircraft</strong> "
        "product development.</p>"
    )
    assert job.posted_at == datetime(2026, 6, 22, tzinfo=UTC)
    assert job.language is None
    assert job.raw == {
        "domain_id": DOMAIN_ID,
        "site_id": 19218,
        "country_iso3": "USA",
        "end_date": "Jun 22, 2031",
        "until_filled": 1,
        "classification": "Operations",
        "workplace_type": "Onsite",
        "pay_details": "DOE",
        "benefits": "Medical, dental, and vision",
        "hide_from_indeed": 0,
    }


def test_listing_only_mode_skips_details(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])

    job = ApplicantProScraper(
        TENANT,
        include_descriptions=False,
        company_name="Kirkhill",
    ).fetch()[0]

    assert job.company == "Kirkhill"
    assert job.description is None
    assert job.ats_id == "kirkhill:4124648"


def test_remote_canadian_job_maps_geography_and_salary(httpx_mock) -> None:
    item = _job()
    item.update(
        iso3="CAN",
        abbreviation="ON",
        jobLocation="Toronto, ON, CAN",
        workplaceType="Remote",
        employmentType="Contract",
        payType="Hourly",
        payTypeFrame="per hour",
        minSalary="75",
        maxSalary="95",
    )
    _mock_listing(httpx_mock, [item])

    job = ApplicantProScraper(TENANT, include_descriptions=False).fetch()[0]

    assert job.country_iso == "CA"
    assert job.region == "North America"
    assert job.is_remote is True
    assert job.salary_currency == "CAD"
    assert job.salary_period == "HOUR"
    assert job.employment_type == "CONTRACT"


def test_euro_salary_uses_mapped_currency(httpx_mock) -> None:
    item = _job()
    item.update(
        iso3="DEU",
        abbreviation="BE",
        jobLocation="Berlin, DEU",
        minSalary="80,000",
        maxSalary="100,000",
    )
    _mock_listing(httpx_mock, [item])

    job = ApplicantProScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()[0]

    assert job.country_iso == "DE"
    assert job.region == "Europe"
    assert job.salary_currency == "EUR"
    assert job.salary_min == 80_000
    assert job.salary_max == 100_000


def test_valid_additional_iso3_code_is_preserved_and_mapped(
    httpx_mock,
) -> None:
    item = _job()
    item.update(
        iso3="ARG",
        abbreviation="C",
        jobLocation="Buenos Aires, ARG",
        minSalary=None,
        maxSalary=None,
        payTypeFrame=None,
        payDetails=None,
    )
    _mock_listing(httpx_mock, [item])

    job = ApplicantProScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()[0]

    assert job.country_iso == "AR"
    assert job.region == "South America"
    assert job.raw["country_iso3"] == "ARG"


@pytest.mark.parametrize(
    ("iso3", "iso2", "region", "currency"),
    [
        ("BFA", "BF", "Africa", "XOF"),
        ("BMU", "BM", "North America", "BMD"),
        ("BOL", "BO", "South America", "BOB"),
        ("ISL", "IS", "Europe", "ISK"),
        ("JOR", "JO", "Asia", "JOD"),
        ("LBN", "LB", "Asia", "LBP"),
        ("MAR", "MA", "Africa", "MAD"),
        ("MLI", "ML", "Africa", "XOF"),
        ("MNP", "MP", "Oceania", "USD"),
        ("MWI", "MW", "Africa", "MWK"),
        ("PRI", "PR", "North America", "USD"),
        ("SLE", "SL", "Africa", "SLE"),
        ("SLV", "SV", "North America", "USD"),
        ("TZA", "TZ", "Africa", "TZS"),
        ("UKR", "UA", "Europe", "UAH"),
    ],
)
def test_all_observed_iso3_codes_map_to_region_and_currency(
    httpx_mock,
    iso3: str,
    iso2: str,
    region: str,
    currency: str,
) -> None:
    item = _job()
    item.update(
        iso3=iso3,
        jobLocation=f"Example, {iso3}",
        minSalary="10",
        maxSalary="20",
    )
    _mock_listing(httpx_mock, [item])

    job = ApplicantProScraper(TENANT, include_descriptions=False).fetch()[0]

    assert job.country_iso == iso2
    assert job.region == region
    assert job.salary_currency == currency


def test_no_compensation_emits_no_currency_or_period_only_summary(
    httpx_mock,
) -> None:
    item = _job()
    item.update(
        minSalary=None,
        maxSalary=None,
        payTypeFrame="per year",
        payDetails=None,
    )
    _mock_listing(httpx_mock, [item])

    job = ApplicantProScraper(
        TENANT,
        include_descriptions=False,
    ).fetch()[0]

    assert job.salary_currency is None
    assert job.salary_period is None
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_summary is None
    assert job.salary is None


def test_empty_active_listing_returns_empty(httpx_mock) -> None:
    _mock_listing(httpx_mock, [])

    assert ApplicantProScraper(TENANT).fetch() == []


def test_count_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREERS_URL, text=_portal())
    httpx_mock.add_response(url=LISTING_URL, json=_listing([_job()], count=2))

    with pytest.raises(ScraperError, match="expected 2 jobs, received 1"):
        ApplicantProScraper(TENANT).fetch()


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job(), _job()])

    with pytest.raises(ScraperError, match="duplicate job id"):
        ApplicantProScraper(TENANT, include_descriptions=False).fetch()


def test_job_disappearing_before_detail_is_dropped(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    httpx_mock.add_response(url=DETAIL_URL, status_code=404)

    assert ApplicantProScraper(TENANT).fetch() == []


def test_transient_detail_failure_retains_listing_data(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    for _ in range(3):
        httpx_mock.add_response(url=DETAIL_URL, status_code=500)

    job = ApplicantProScraper(TENANT).fetch()[0]

    assert job.ats_id == "kirkhill:4124648"
    assert job.description is None


def test_unsuccessful_detail_payload_fails_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    httpx_mock.add_response(
        url=DETAIL_URL,
        json={"success": False, "data": None},
    )

    with pytest.raises(ScraperError, match="unsuccessful detail"):
        ApplicantProScraper(TENANT).fetch()


def test_malformed_detail_json_fails_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    httpx_mock.add_response(
        url=DETAIL_URL,
        text="<html>maintenance</html>",
    )

    with pytest.raises(ScraperError, match="not valid JSON"):
        ApplicantProScraper(TENANT).fetch()


def test_detail_id_mismatch_fails_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_job()])
    httpx_mock.add_response(url=DETAIL_URL, json=_detail(999))

    with pytest.raises(ScraperError, match="did not match"):
        ApplicantProScraper(TENANT).fetch()


def test_missing_public_portal_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=CAREERS_URL,
        text="<html><title>ApplicantPro</title></html>",
    )

    with pytest.raises(CompanyNotFoundError):
        ApplicantProScraper(TENANT).fetch()


def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREERS_URL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        ApplicantProScraper(TENANT).fetch()
