"""Tests for the Paylocity public recruiting scraper."""

from __future__ import annotations

import json

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.paylocity import (
    PaylocityScraper,
    _extract_page_data,
    _normalize_board_id,
)

BOARD_ID = "8e0feae7-e42f-437e-97b1-53b917185eed"
LISTING_URL = (
    "https://recruiting.paylocity.com/Recruiting/Jobs/All/" + BOARD_ID
)


def _row(
    job_id: int,
    *,
    title: str = "Platform Engineer",
    internal: bool = False,
) -> dict[str, object]:
    return {
        "JobId": job_id,
        "JobTitle": title,
        "LocationName": "HQ",
        "PublishedDate": "2026-07-24T14:16:35-05:00",
        "IsInternal": internal,
        "HiringDepartment": "Engineering",
        "JobLocation": {
            "LocationId": 10,
            "City": "Houston",
            "State": "TX",
            "Country": "USA",
        },
        "IsRemote": False,
        "IndeedRemoteType": 2,
    }


def _listing(rows: list[dict[str, object]]) -> str:
    payload = {
        "Jobs": rows,
        "ModuleId": "19907",
        "ModuleTitle": "Career Opportunities",
    }
    return (
        "<html><script>window.pageData = "
        + json.dumps(payload)
        + ";</script></html>"
    )


def _detail(
    *,
    title: str = "Platform Engineer",
    company: str = "Acme Corp",
) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": "2026-07-24T19:19:35-05:00",
        "hiringOrganization": {
            "@type": "Organization",
            "name": company,
        },
        "description": "<p>Build reliable systems.</p>",
        "employmentType": "FULL_TIME",
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Austin",
                "addressRegion": "TX",
                "addressCountry": "US",
            },
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 100000,
                "maxValue": 130000,
                "unitText": "YEAR",
            },
        },
    }
    return (
        '<html><script type="application/ld+json">'
        + json.dumps(posting)
        + "</script></html>"
    )


def _legacy_detail() -> str:
    return """
    <html>
      <div class="job-preview-details">
        <div class="job-listing-header">Description</div>
        <div><p>Repair heavy trucks.</p></div>
        <div class="job-listing-header">Requirements</div>
        <div><ul><li>Diesel experience</li></ul></div>
      </div>
    </html>
    """


def test_fetches_external_jobs_and_hydrates_details(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_listing([
        _row(4363000),
        _row(4363001, title="Internal Role", internal=True),
    ]))
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363000",
        text=_detail(),
    )

    jobs = PaylocityScraper(BOARD_ID, company_name="Acme").fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.PAYLOCITY
    assert job.ats_id == f"{BOARD_ID}:4363000"
    assert job.company == "Acme Corp"
    assert job.description == "<p>Build reliable systems.</p>"
    assert job.location == "Austin, TX, US"
    assert job.country_iso == "US"
    assert job.department == "Engineering"
    assert job.employment_type == "FULL_TIME"
    assert job.salary_min == 100000
    assert job.salary_max == 130000
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEAR"
    assert str(job.apply_url).endswith("/Recruiting/Jobs/Apply/4363000")


def test_hydrates_legacy_detail_sections_without_json_ld(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_listing([_row(4363000)]))
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363000",
        text=_legacy_detail(),
    )

    jobs = PaylocityScraper(BOARD_ID, company_name="Acme").fetch()

    assert len(jobs) == 1
    assert jobs[0].description == (
        "<p>Description</p><p>Repair heavy trucks.</p>"
        "<p>Requirements</p><ul><li>Diesel experience</li></ul>"
    )


def test_malformed_detail_retains_listing_and_adjacent_job_enriches(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_listing([
            _row(4363000),
            _row(4363001, title="Designer"),
        ]),
    )
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363000",
        text="<html><div class='job-preview-details'></div></html>",
    )
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363001",
        text=_detail(title="Product Designer"),
    )

    jobs = PaylocityScraper(BOARD_ID, company_name="Acme").fetch()

    assert len(jobs) == 2
    assert jobs[0].company == "Acme"
    assert jobs[0].description is None
    assert jobs[0].location == "Houston, TX, US"
    assert jobs[1].title == "Product Designer"
    assert jobs[1].description == "<p>Build reliable systems.</p>"


def test_include_descriptions_false_skips_detail(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_listing([_row(4363000)]))

    jobs = PaylocityScraper(
        BOARD_ID,
        include_descriptions=False,
        company_name="Acme",
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].description is None
    assert jobs[0].location == "Houston, TX, US"


def test_closed_job_between_listing_and_detail_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_listing([
        _row(4363000),
        _row(4363001, title="Designer"),
    ]))
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363000",
        text=_detail(),
    )
    httpx_mock.add_response(
        url="https://recruiting.paylocity.com/Recruiting/Jobs/Details/4363001",
        status_code=404,
    )

    jobs = PaylocityScraper(BOARD_ID).fetch()

    assert [job.requisition_id for job in jobs] == ["4363000"]


def test_transient_detail_failure_retains_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=LISTING_URL, text=_listing([_row(4363000)]))
    for _ in range(3):
        httpx_mock.add_response(
            url=(
                "https://recruiting.paylocity.com/Recruiting/Jobs/Details/"
                "4363000"
            ),
            status_code=500,
        )

    jobs = PaylocityScraper(BOARD_ID, company_name="Acme").fetch()

    assert len(jobs) == 1
    assert jobs[0].company == "Acme"
    assert jobs[0].description is None


def test_duplicate_external_job_ids_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LISTING_URL,
        text=_listing([_row(4363000), _row(4363000)]),
    )

    with pytest.raises(ScraperError, match="duplicate job id"):
        PaylocityScraper(BOARD_ID, include_descriptions=False).fetch()


@pytest.mark.parametrize(
    "html_text",
    [
        "<html></html>",
        "<script>window.pageData = nope;</script>",
        '<script>window.pageData = {"Jobs":[]}</script>',
        (
            '<script>window.pageData = {"Jobs":[]};</script>'
            '<script>window.pageData = {"Jobs":[]};</script>'
        ),
    ],
)
def test_malformed_page_data_fails_closed(html_text: str) -> None:
    with pytest.raises(ScraperError):
        _extract_page_data(html_text)


def test_listing_requires_explicit_external_flag(httpx_mock) -> None:
    row = _row(4363000)
    row.pop("IsInternal")
    httpx_mock.add_response(url=LISTING_URL, text=_listing([row]))

    with pytest.raises(ScraperError, match="external-job flag"):
        PaylocityScraper(BOARD_ID, include_descriptions=False).fetch()


@pytest.mark.parametrize(
    "value",
    [
        "not-a-uuid",
        "https://evil.example/Recruiting/Jobs/All/" + BOARD_ID,
        "http://recruiting.paylocity.com/Recruiting/Jobs/All/" + BOARD_ID,
        (
            "https://recruiting.paylocity.com/Recruiting/Jobs/Details/"
            "4363000"
        ),
        LISTING_URL + "?internal=true",
    ],
)
def test_rejects_untrusted_board_identifiers(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_board_id(value)


def test_normalizes_trusted_listing_url() -> None:
    assert _normalize_board_id(LISTING_URL) == BOARD_ID
