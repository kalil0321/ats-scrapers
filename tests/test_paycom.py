from __future__ import annotations

import base64
import json

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import PaycomScraper
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.paycom import (
    _extract_config,
    _normalize_portal_token,
)

TOKEN = "0" * 32
CLIENT_CODE = "0TS15"
PORTAL_URL = (
    "https://www.paycomonline.net/v4/ats/web.php/portal/"
    f"{TOKEN}/career-page"
)
SEARCH_URL = (
    "https://portal-applicant-tracking.us-cent.paycomonline.net"
    "/api/ats/job-posting-previews/search"
)


def _jwt(client_code: str = CLIENT_CODE) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"clientcode": client_code}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def _bootstrap(
    *,
    service_url: str = (
        "https://portal-applicant-tracking.us-cent.paycomonline.net/"
    ),
) -> str:
    config = {
        "sessionJWT": _jwt(),
        "libConfig": json.dumps(
            {
                "atsPortalMantleServiceUrl": service_url,
                "locale": "en-US",
                "translationHighlights": False,
            }
        ),
    }
    return (
        "<html><script>var configsFromHost = "
        + json.dumps(config)
        + "; var Mountable = {};</script></html>"
    )


def _preview(job_id: int, title: str = "Groundskeeper") -> dict[str, object]:
    return {
        "jobId": job_id,
        "jobTitle": title,
        "positionType": "Seasonal Jobs",
        "remoteType": "",
        "locations": "Estes Park, CO 80511",
        "description": "Maintain safe and welcoming grounds.",
        "postedOn": "",
        "isHotJob": True,
    }


def _search(
    rows: list[dict[str, object]],
    *,
    count: int | None = None,
) -> dict[str, object]:
    return {
        "jobPostingPreviews": rows,
        "jobPostingPreviewsCount": len(rows) if count is None else count,
    }


def _detail(
    job_id: int,
    *,
    company: str = "YMCA OF THE ROCKIES",
) -> dict[str, object]:
    google_job = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Grounds Maintenance Staff",
        "identifier": f"J{CLIENT_CODE}{job_id}",
        "datePosted": "2025-11-21",
        "employmentType": "OTHER",
        "hiringOrganization": {
            "@type": "Organization",
            "name": company,
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "addressLocality": "Estes Park",
                "addressRegion": "CO",
                "postalCode": "80511",
                "addressCountry": "USA",
            },
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 15.16,
                "maxValue": 18.5,
                "unitText": "",
            },
        },
    }
    return {
        "jobPosting": {
            "jobId": job_id,
            "clientCode": CLIENT_CODE,
            "jobTitle": "Grounds Maintenance Staff",
            "location": "Estes Park Center - Estes Park, CO 80511",
            "secondaryLocations": ["Snow Mountain Ranch, CO"],
            "remoteType": "",
            "isHotJob": True,
            "salaryRange": "$15.16 - $18.50 Hourly",
            "positionType": "Seasonal Jobs",
            "jobShift": "Day",
            "educationLevel": "High School",
            "travelPercentage": "None",
            "jobCategory": "Buildings and Grounds",
            "description": "<p>Maintain and improve landscaped areas.</p>",
            "descriptionTitle": "Description",
            "qualifications": "<ul><li>Valid driver license.</li></ul>",
            "qualificationsTitle": "Qualifications",
            "googleJobJson": json.dumps(google_job),
        }
    }


def _mock_listing(httpx_mock, rows: list[dict[str, object]]) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(url=SEARCH_URL, json=_search(rows))


def test_registry_resolves_paycom() -> None:
    assert ScraperRegistry.get(ATSType.PAYCOM) is PaycomScraper


def test_fetches_and_hydrates_rich_job_details(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(466263)])
    httpx_mock.add_response(
        url=(
            "https://portal-applicant-tracking.us-cent.paycomonline.net"
            "/api/ats/job-postings/466263"
        ),
        json=_detail(466263),
    )

    jobs = PaycomScraper(TOKEN, company_name="Fallback").fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.PAYCOM
    assert job.ats_id == f"{CLIENT_CODE}:466263"
    assert job.company == "YMCA OF THE ROCKIES"
    assert job.title == "Grounds Maintenance Staff"
    assert job.location == (
        "Estes Park Center - Estes Park, CO 80511; Snow Mountain Ranch, CO"
    )
    assert job.country_iso == "US"
    assert job.region == "North America"
    assert job.description == (
        "<h2>Description</h2><p>Maintain and improve landscaped areas.</p>\n"
        "<h2>Qualifications</h2><ul><li>Valid driver license.</li></ul>"
    )
    assert job.salary_summary == "$15.16 - $18.50 Hourly"
    assert job.salary_currency == "USD"
    assert job.salary_min == 15.16
    assert job.salary_max == 18.5
    assert job.salary_period == "HOUR"
    assert job.employment_type == "TEMPORARY"
    assert job.commitment == "Seasonal Jobs"
    assert job.department == "Buildings and Grounds"
    assert job.requisition_id == f"J{CLIENT_CODE}466263"
    assert job.posted_at.isoformat() == "2025-11-21T00:00:00+00:00"
    assert str(job.apply_url) == (
        "https://www.paycomonline.net/v4/ats/web.php/portal/"
        f"{TOKEN}/jobs/466263"
    )


def test_listing_only_mode_skips_details(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(466263)])

    job = PaycomScraper(
        TOKEN,
        include_descriptions=False,
        company_name="YMCA",
    ).fetch()[0]

    assert job.company == "YMCA"
    assert job.description is None
    assert job.ats_id == f"{CLIENT_CODE}:466263"


def test_paginates_until_reported_count(httpx_mock) -> None:
    first_page = [_preview(index) for index in range(1, 101)]
    final_page = [_preview(101)]
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(
        url=SEARCH_URL,
        json=_search(first_page, count=101),
    )
    httpx_mock.add_response(
        url=SEARCH_URL,
        json=_search(final_page, count=101),
    )

    jobs = PaycomScraper(
        TOKEN,
        include_descriptions=False,
        company_name="Acme",
    ).fetch()

    assert len(jobs) == 101
    assert len({job.ats_id for job in jobs}) == 101


def test_search_count_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL_URL, text=_bootstrap())
    httpx_mock.add_response(
        url=SEARCH_URL,
        json=_search([_preview(1)], count=2),
    )
    httpx_mock.add_response(
        url=SEARCH_URL,
        json=_search([], count=2),
    )

    with pytest.raises(ScraperError, match="expected 2 jobs"):
        PaycomScraper(TOKEN, include_descriptions=False).fetch()


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(1), _preview(1)])

    with pytest.raises(ScraperError, match="duplicate job IDs"):
        PaycomScraper(TOKEN, include_descriptions=False).fetch()


def test_detail_retries_transient_statuses(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(466263)])
    detail_url = (
        "https://portal-applicant-tracking.us-cent.paycomonline.net"
        "/api/ats/job-postings/466263"
    )
    httpx_mock.add_response(url=detail_url, status_code=503)
    httpx_mock.add_response(url=detail_url, status_code=503)
    httpx_mock.add_response(url=detail_url, json=_detail(466263))

    job = PaycomScraper(TOKEN).fetch()[0]

    assert job.description.startswith("<h2>Description</h2>")
    assert job.company == "YMCA OF THE ROCKIES"


def test_closed_job_between_listing_and_detail_is_removed(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(1), _preview(2)])
    detail_base = (
        "https://portal-applicant-tracking.us-cent.paycomonline.net"
        "/api/ats/job-postings/"
    )
    httpx_mock.add_response(url=detail_base + "1", json=_detail(1))
    httpx_mock.add_response(url=detail_base + "2", status_code=404)

    jobs = PaycomScraper(TOKEN).fetch()

    assert [job.ats_id for job in jobs] == [f"{CLIENT_CODE}:1"]


def test_malformed_detail_retains_valid_listing(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(1)])
    httpx_mock.add_response(
        url=(
            "https://portal-applicant-tracking.us-cent.paycomonline.net"
            "/api/ats/job-postings/1"
        ),
        json={"unexpected": True},
    )

    job = PaycomScraper(TOKEN, company_name="Acme").fetch()[0]

    assert job.company == "Acme"
    assert job.description == "Maintain safe and welcoming grounds."


def test_malformed_google_metadata_preserves_direct_details(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(1)])
    payload = _detail(1)
    payload["jobPosting"]["googleJobJson"] = "{invalid"
    httpx_mock.add_response(
        url=(
            "https://portal-applicant-tracking.us-cent.paycomonline.net"
            "/api/ats/job-postings/1"
        ),
        json=payload,
    )

    job = PaycomScraper(TOKEN, company_name="Acme").fetch()[0]

    assert job.company == "Acme"
    assert job.title == "Grounds Maintenance Staff"
    assert job.description.startswith("<h2>Description</h2>")
    assert job.location.startswith("Estes Park Center")
    assert job.salary_summary == "$15.16 - $18.50 Hourly"
    assert job.department == "Buildings and Grounds"


def test_single_value_salary_populates_both_bounds(httpx_mock) -> None:
    _mock_listing(httpx_mock, [_preview(1)])
    payload = _detail(1)
    google_job = json.loads(payload["jobPosting"]["googleJobJson"])
    google_job["baseSalary"]["value"] = {
        "@type": "QuantitativeValue",
        "value": 19.5,
        "unitText": "HOUR",
    }
    payload["jobPosting"]["googleJobJson"] = json.dumps(google_job)
    httpx_mock.add_response(
        url=(
            "https://portal-applicant-tracking.us-cent.paycomonline.net"
            "/api/ats/job-postings/1"
        ),
        json=payload,
    )

    job = PaycomScraper(TOKEN).fetch()[0]

    assert job.salary_min == 19.5
    assert job.salary_max == 19.5
    assert job.salary_period == "HOUR"


def test_untrusted_bootstrap_service_is_rejected(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL_URL,
        text=_bootstrap(service_url="https://evil.example/"),
    )

    with pytest.raises(ScraperError, match="untrusted API service URL"):
        PaycomScraper(TOKEN).fetch()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-token",
        "../00f1f305d986350f2a5df3d1ae79350f",
        (
            "https://evil.example/v4/ats/web.php/portal/"
            f"{TOKEN}/career-page"
        ),
        (
            "http://www.paycomonline.net/v4/ats/web.php/portal/"
            f"{TOKEN}/career-page"
        ),
        (
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            f"{TOKEN}/career-page?internal=true"
        ),
        (
            "https://www.paycomonline.net:invalid/v4/ats/web.php/portal/"
            f"{TOKEN}/career-page"
        ),
    ],
)
def test_rejects_untrusted_portal_identifiers(value: str) -> None:
    with pytest.raises(ScraperError):
        PaycomScraper(value)


def test_normalizes_trusted_portal_url() -> None:
    assert _normalize_portal_token(PORTAL_URL.replace(TOKEN, TOKEN.upper())) == TOKEN


@pytest.mark.parametrize(
    "html_text",
    [
        "<html></html>",
        "<script>var configsFromHost = nope; var Mountable</script>",
        "<script>var configsFromHost = []; var Mountable</script>",
    ],
)
def test_malformed_bootstrap_config_fails_closed(html_text: str) -> None:
    with pytest.raises(ScraperError):
        _extract_config(html_text)
