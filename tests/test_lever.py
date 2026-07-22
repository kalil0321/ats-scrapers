"""Tests for the Lever scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import LeverScraper, ScraperRegistry

API = "https://api.lever.co/v0/postings/acme?mode=json"


# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture
# in conftest.py — the shared fetch layer replaced per-scraper retry
# constants.


def _job(jid: str = "x1", text: str = "SWE",
         location: str = "Remote") -> dict:
    return {
        "id": jid,
        "text": text,
        "hostedUrl": f"https://jobs.lever.co/acme/{jid}",
        "categories": {"location": location, "team": "Eng"},
        "createdAt": 1714521600000,  # ~2026-04-30
    }


def test_registry_resolves_lever() -> None:
    assert ScraperRegistry.get(ATSType.LEVER) is LeverScraper


def test_parses_basic_job(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=[_job()])
    jobs = LeverScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "SWE"
    assert job.company == "acme"
    assert job.location == "Remote"
    assert job.ats_id == "x1"


def test_returns_empty_list(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=[])
    assert LeverScraper("acme").fetch() == []


def test_404_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        LeverScraper("acme").fetch()


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=502)
    httpx_mock.add_response(url=API, json=[_job()])
    assert len(LeverScraper("acme").fetch()) == 1
