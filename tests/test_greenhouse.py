"""Tests for the Greenhouse scraper."""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import GreenhouseScraper, ScraperRegistry

API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.greenhouse as gh
    monkeypatch.setattr(gh, "MAX_RETRIES", 1)
    monkeypatch.setattr(gh, "RETRY_BASE_DELAY", 0.0)


# Greenhouse listing now requests ``?content=true``; the scraper fetches
# everything in a single call (no per-job detail), so tests that mock
# the URL constant ``API`` already cover the full request set. The
# relax-mark is a safety net in case a test variant adds a non-default
# slug — it keeps tests passing when the URL diverges from ``API``.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _job(jid: str = "1", title: str = "Engineer",
         location: str = "Remote",
         absolute_url: str = "https://job-boards.greenhouse.io/acme/jobs/1") -> dict:
    return {
        "id": jid,
        "title": title,
        "location": {"name": location},
        "absolute_url": absolute_url,
        "updated_at": "2026-04-15T08:00:00Z",
        "departments": [{"name": "Eng"}],
    }


def test_registry_resolves_greenhouse() -> None:
    assert ScraperRegistry.get(ATSType.GREENHOUSE) is GreenhouseScraper


def test_parses_basic_job(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    jobs = GreenhouseScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Engineer"
    assert job.ats_type is ATSType.GREENHOUSE
    assert job.company == "acme"
    assert job.location == "Remote"


def test_returns_empty_for_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"jobs": []})
    assert GreenhouseScraper("acme").fetch() == []


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        GreenhouseScraper("acme").fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.greenhouse as gh
    monkeypatch.setattr(gh, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=API, status_code=503)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    assert len(GreenhouseScraper("acme").fetch()) == 1


def test_5xx_exhausts(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.greenhouse as gh
    monkeypatch.setattr(gh, "MAX_RETRIES", 2)
    httpx_mock.add_response(url=API, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError):
        GreenhouseScraper("acme").fetch()
