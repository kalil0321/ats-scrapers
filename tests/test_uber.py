"""Tests for Uber's current careers JSON API."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import UberScraper

API_URL = "https://jobs.uber.com/api/jobs/search/"


def _job(job_id: str) -> dict[str, object]:
    return {
        "Id": job_id,
        "Title": f"Job {job_id}",
        "Reference": f"R-{job_id}",
        "Description": "<p>Hello <strong>world</strong></p>",
        "DisplayDate": "2026-08-22T23:48:24Z",
        "AdditionalText": "University Operations",
        "Teams": ["University"],
        "ContractType": "Full time",
        "WorkPattern": "Intern",
        "Remote": False,
        "Salary": {
            "MinValue": 50_000,
            "MaxValue": 70_000,
            "Currency": "usd",
            "Period": "Year",
            "Description": "$50,000-$70,000",
        },
        "Locations": [
            {
                "Address": "Paris, France",
                "City": "Paris",
                "Region": "Ile-de-France",
                "Country": "France",
                "CountryCode": "FR",
                "LocationPoint": {"coordinates": [2.3522, 48.8566]},
            },
            {
                "Address": "Amsterdam, Netherlands",
                "City": "Amsterdam",
                "Region": "North Holland",
                "Country": "Netherlands",
                "CountryCode": "NL",
                "LocationPoint": {"coordinates": [4.9041, 52.3676]},
            },
        ],
        "Urls": [{"Culture": "en-us", "Url": f"/en/jobs/{job_id}/", "IsDefault": True}],
    }


def _payload(
    jobs: list[dict[str, object]],
    *,
    total_jobs: int | None = None,
    total_pages: int = 1,
    page: int = 1,
) -> dict[str, object]:
    return {
        "jobs": jobs,
        "totalJobs": len(jobs) if total_jobs is None else total_jobs,
        "totalPages": total_pages,
        "page": page,
        "pageSize": 1000,
    }


def _use_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(UberScraper, "fetch_engine", "httpx")


def test_parses_current_api_fields(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json=_payload([_job("300886")]),
    )

    jobs = UberScraper("uber").fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_type is ATSType.UBER
    assert job.ats_id == "300886"
    assert job.title == "Job 300886"
    assert job.location == "Paris, France; Amsterdam, Netherlands"
    assert job.country_iso == "FR"
    assert job.region is None
    assert (job.lat, job.lon) == (48.8566, 2.3522)
    assert job.is_remote is False
    assert job.department == "University Operations"
    assert job.team == "University"
    assert job.employment_type == "INTERN"
    assert job.commitment == "Full time / Intern"
    assert job.requisition_id == "R-300886"
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEAR"
    assert job.description == "Hello\nworld"
    assert str(job.url) == "https://jobs.uber.com/en/jobs/300886/"


def test_can_omit_descriptions(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json=_payload([_job("1")]),
    )

    assert UberScraper("uber", include_descriptions=False).fetch()[0].description is None


def test_paginates_to_advertised_total(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json=_payload([_job("1")], total_jobs=2, total_pages=2),
    )
    httpx_mock.add_response(
        url=f"{API_URL}?page=2&pagesize=1000",
        json=_payload([_job("2")], total_jobs=2, total_pages=2, page=2),
    )

    assert len(UberScraper("uber").fetch()) == 2


def test_rejects_partial_catalogue(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json=_payload([_job("1")], total_jobs=2),
    )

    with pytest.raises(
        ScraperError,
        match="advertised 2 jobs but returned 1 unique IDs",
    ):
        UberScraper("uber").fetch()


def test_rejects_duplicate_job_ids(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json=_payload([_job("1"), _job("1")]),
    )

    with pytest.raises(ScraperError, match="duplicate job ID 1"):
        UberScraper("uber").fetch()


def test_rejects_invalid_payload(httpx_mock, monkeypatch) -> None:
    _use_mock_transport(monkeypatch)
    httpx_mock.add_response(
        url=f"{API_URL}?page=1&pagesize=1000",
        json={"message": "blocked"},
    )

    with pytest.raises(ScraperError, match="invalid jobs payload"):
        UberScraper("uber").fetch()


def test_pipeline_fails_closed_on_empty() -> None:
    from scripts.run_pipeline import CONFIGS

    assert CONFIGS["uber"]["fail_closed_on_empty"] is True
