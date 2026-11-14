"""Tests for the Ashby scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import AshbyScraper, ScraperRegistry

API = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"


# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture
# in conftest.py — the shared fetch layer replaced per-scraper retry
# constants.


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
    """A board that is gone answers 404 on the API and on the hosted page."""
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url="https://jobs.ashbyhq.com/acme", status_code=404)
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


def test_5xx_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=503)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    assert len(AshbyScraper("acme").fetch()) == 1


# --- hosted board: the API is off for this employer, the page is public -----

BOARD = "https://jobs.ashbyhq.com/acme"


def _page(data: dict) -> str:
    import json

    return (
        "<html><head><script>window.__appData = "
        + json.dumps(data)
        + ';window.other = {"a": 1};</script></head><body></body></html>'
    )


def _board(*postings: dict) -> str:
    return _page({"organization": {"name": "Acme"}, "jobBoard": {"jobPostings": list(postings)}})


def _posting(jid: str = "p1", title: str = "Account Executive") -> dict:
    return {
        "id": jid,
        "title": title,
        "locationName": "San Francisco, CA",
        "workplaceType": "Hybrid",
        "employmentType": "FullTime",
        "secondaryLocations": [{"locationName": "New York, NY"}],
        "compensationTierSummary": None,
    }


def _detail(jid: str = "p1", title: str = "Account Executive") -> str:
    return _page(
        {
            "posting": {
                "id": jid,
                "title": title,
                "departmentName": "Go To Market",
                "teamNames": ["Go To Market", "Sales"],
                "locationName": "San Francisco, CA",
                "secondaryLocationNames": ["New York, NY", "Los Angeles, CA"],
                "workplaceType": "Hybrid",
                "employmentType": "FullTime",
                "descriptionHtml": "<p>Sell things.</p>",
                "compensationTierSummary": "$120K – $135K • Offers Equity",
                "scrapeableCompensationSalarySummary": "$120K - $135K",
            }
        }
    )


def test_api_404_reads_the_hosted_board(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text=_board(_posting("p1"), _posting("p2", "Designer")))
    httpx_mock.add_response(url=f"{BOARD}/p1", text=_detail("p1"))
    httpx_mock.add_response(url=f"{BOARD}/p2", text=_detail("p2", "Designer"))

    jobs = AshbyScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["p1", "p2"]
    job = jobs[0]
    assert job.title == "Account Executive"
    assert str(job.url) == f"{BOARD}/p1"
    assert str(job.apply_url) == f"{BOARD}/p1/application"
    assert job.location == "San Francisco, CA"
    assert job.description == "<p>Sell things.</p>"
    assert job.department == "Go To Market"
    assert job.team == "Sales"
    assert job.employment_type == "FULL_TIME"
    assert job.is_remote is None
    assert job.salary_summary == "$120K – $135K • Offers Equity"
    assert job.posted_at is None  # the page carries no publication date
    assert job.raw["source"] == "hosted_board"
    assert job.raw["secondary_locations"] == ["New York, NY", "Los Angeles, CA"]
    assert jobs[1].title == "Designer"


def test_api_jobs_never_touch_the_hosted_board(httpx_mock) -> None:
    """Only the API is called when it answers; an unexpected request to the
    page would fail this test."""
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})

    (job,) = AshbyScraper("acme").fetch()

    assert "source" not in (job.raw or {})


def test_hosted_page_without_a_board_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text=_page({"organization": None, "jobBoard": None}))

    with pytest.raises(CompanyNotFoundError):
        AshbyScraper("acme").fetch()


def test_hosted_board_without_openings_is_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text=_board())

    assert AshbyScraper("acme").fetch() == []


def test_hosted_page_without_app_data_fails_closed(httpx_mock) -> None:
    """A page we cannot read is an error, not an empty board: the previous
    run's jobs must not be dropped because Ashby changed its markup."""
    from ats_scrapers.exceptions import ScraperError

    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text="<html><body>Jobs</body></html>")

    with pytest.raises(ScraperError) as excinfo:
        AshbyScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_posting_closed_between_listing_and_detail_is_skipped(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text=_board(_posting("p1"), _posting("p2")))
    httpx_mock.add_response(url=f"{BOARD}/p1", status_code=404)
    httpx_mock.add_response(url=f"{BOARD}/p2", text=_detail("p2"))

    assert [job.ats_id for job in AshbyScraper("acme").fetch()] == ["p2"]


def test_posting_page_without_a_posting_fails_closed(httpx_mock) -> None:
    from ats_scrapers.exceptions import ScraperError

    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=BOARD, text=_board(_posting("p1")))
    httpx_mock.add_response(url=f"{BOARD}/p1", text=_page({"posting": None}))

    with pytest.raises(ScraperError):
        AshbyScraper("acme").fetch()


def test_hosted_board_of_a_slug_with_a_space(httpx_mock) -> None:
    """Catalogue slugs are stored percent-encoded (``marathon%20fusion``)."""
    slug = "marathon%20fusion"
    httpx_mock.add_response(
        url=f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        status_code=404,
    )
    httpx_mock.add_response(url=f"https://jobs.ashbyhq.com/{slug}", text=_board(_posting("p1")))
    httpx_mock.add_response(url=f"https://jobs.ashbyhq.com/{slug}/p1", text=_detail("p1"))

    (job,) = AshbyScraper(slug).fetch()

    assert str(job.url) == f"https://jobs.ashbyhq.com/{slug}/p1"
