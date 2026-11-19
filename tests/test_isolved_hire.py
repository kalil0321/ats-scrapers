from __future__ import annotations

from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ApplicantProScraper, IsolvedHireScraper
from ats_scrapers.scrapers.base import ScraperRegistry

TENANT = "accel"
DOMAIN_ID = "11454"
CAREERS_URL = f"https://{TENANT}.isolvedhire.com/jobs/"
LISTING_URL = f"https://{TENANT}.isolvedhire.com/core/jobs/{DOMAIN_ID}?getParams=%7B%7D"
DETAIL_URL = f"https://{TENANT}.isolvedhire.com/core/jobs/{DOMAIN_ID}/1907340/job-details"

PORTAL = """
<html>
  <head><title>Job Listings - ACCEL Jobs</title></head>
  <body><script>const app = { componentData: { domainId : 11454 } };</script></body>
</html>
"""


def _job() -> dict[str, object]:
    return {
        "id": 1907340,
        "title": "Direct Support Provider",
        "city": "Phoenix",
        "subdomain": TENANT,
        "iso3": "USA",
        "abbreviation": "AZ",
        "siteId": 11454,
        "startDateRef": "Nov 3, 2026",
        "workplaceType": "Onsite",
        "employmentType": "Full Time",
        "jobLocation": "3310 West Cheryl Drive, Phoenix, AZ, USA",
    }


def _mock_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREERS_URL, text=PORTAL)
    httpx_mock.add_response(
        url=LISTING_URL,
        json={"success": True, "message": "", "data": {"jobs": [_job()], "jobCount": 1}},
    )


def test_registry_resolves_isolved_hire() -> None:
    assert ScraperRegistry.get(ATSType.ISOLVED_HIRE) is IsolvedHireScraper
    assert ScraperRegistry.get(ATSType.APPLICANTPRO) is ApplicantProScraper


def test_reads_the_portal_on_its_own_host(httpx_mock) -> None:
    _mock_listing(httpx_mock)

    job = IsolvedHireScraper(TENANT, include_descriptions=False).fetch()[0]

    assert job.ats_type is ATSType.ISOLVED_HIRE
    assert job.ats_id == "accel:1907340"
    assert job.company == "ACCEL"
    assert job.title == "Direct Support Provider"
    assert str(job.url) == "https://accel.isolvedhire.com/jobs/1907340"
    assert str(job.apply_url) == str(job.url)
    assert job.country_iso == "US"


def test_reads_the_description_on_its_own_host(httpx_mock) -> None:
    _mock_listing(httpx_mock)
    httpx_mock.add_response(
        url=DETAIL_URL,
        json={
            "success": True,
            "message": "",
            "data": {"id": 1907340, "title": "Direct Support Provider", "description": "<p>Support adults.</p>"},
        },
    )

    job = IsolvedHireScraper(TENANT).fetch()[0]

    assert job.description == "Support adults."
