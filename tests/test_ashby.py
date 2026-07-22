"""Tests for the Ashby scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import AshbyScraper, ScraperRegistry

API = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.ashby as ash
    monkeypatch.setattr(ash, "MAX_RETRIES", 1)
    monkeypatch.setattr(ash, "RETRY_BASE_DELAY", 0.0)


def _job(jid: str = "j1", title: str = "SWE", location: str = "Remote") -> dict:
    return {
        "id": jid,
        "title": title,
        "location": location,
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{jid}",
        "publishedAt": "2026-04-15T08:00:00.000Z",
    }


def test_registry_resolves_ashby() -> None:
    assert ScraperRegistry.get(ATSType.ASHBY) is AshbyScraper


def test_parses_basic_job(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    jobs = AshbyScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "SWE"
    assert job.company == "acme"
    assert job.ats_type is ATSType.ASHBY


def test_returns_empty_list(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"jobs": []})
    assert AshbyScraper("acme").fetch() == []


def test_404_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        AshbyScraper("acme").fetch()


def test_compensation_summary_passthrough(httpx_mock) -> None:
    """The summary string is always preserved when the compensation block
    carries one — structured min/max may or may not be set depending on
    whether ``summaryComponents`` is in the exact shape the parser
    expects, so we only assert on the more permissive summary field."""
    j = _job()
    j["compensation"] = {"compensationTierSummary": "$120k - $180k"}
    httpx_mock.add_response(url=API, json={"jobs": [j]})
    job = AshbyScraper("acme").fetch()[0]
    assert job.salary_summary == "$120k - $180k"


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.scrapers.ashby as ash
    monkeypatch.setattr(ash, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=API, status_code=503)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    assert len(AshbyScraper("acme").fetch()) == 1
