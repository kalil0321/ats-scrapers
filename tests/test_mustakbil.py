"""Tests for Mustakbil Pakistan."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import MustakbilScraper, ScraperRegistry

_API_RE = re.compile(r"^https://api-public\.mustakbil\.com/ws/jobs/search/")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.mustakbil as m

    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


def _job(job_id: int, title: str = "Sales Executive") -> dict:
    return {
        "id": job_id,
        "employerId": 128081,
        "title": title,
        "category": "Sales",
        "type": "Full Time",
        "shift": "Morning Shift",
        "experienceLevel": "1 Year",
        "salaryMin": 40000,
        "salaryMax": 100000,
        "currency": "PKR",
        "description": "Drive B2B sales.",
        "cities": "Rawalpindi",
        "city": "Rawalpindi",
        "country": "Pakistan",
        "postedOn": "2026-04-28T09:21:00",
        "lastDate": "2026-07-28T00:16:00",
        "adType": "Premium",
        "company": "Mend Skincare",
        "vacancies": 1,
        "telecommute": False,
        "urlTitle": "sales-executive",
        "urlCountry": "pakistan",
        "urlCity": "rawalpindi",
    }


def test_registry_resolves_mustakbil() -> None:
    assert ScraperRegistry.get(ATSType.MUSTAKBIL) is MustakbilScraper


def test_parses_mustakbil_job_payload(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json={"list": [_job(1431768)]})
    httpx_mock.add_response(url=_API_RE, json={"list": []})

    jobs = MustakbilScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.MUSTAKBIL
    assert j.ats_id == "1431768"
    assert j.title == "Sales Executive"
    assert j.company == "Mend Skincare"
    assert j.location == "Rawalpindi, Pakistan"
    assert j.country_iso == "PK"
    assert j.employment_type == "FULL_TIME"
    assert j.salary_currency == "PKR"
    assert j.salary_min == 40000
    assert j.salary_max == 100000
    assert j.posted_at is not None
    assert str(j.url) == "https://www.mustakbil.com/jobs/job/1431768/pakistan/rawalpindi/sales-executive"


def test_paginates_until_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json={"list": [_job(1), _job(2)]})
    httpx_mock.add_response(url=_API_RE, json={"list": [_job(3)]})
    httpx_mock.add_response(url=_API_RE, json={"list": []})

    jobs = MustakbilScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        MustakbilScraper("any").fetch()
