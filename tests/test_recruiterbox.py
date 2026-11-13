"""Tests for the Recruiterbox / Trakstar Hire scraper."""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import RecruiterboxScraper, ScraperRegistry

API = "https://jsapi.recruiterbox.com/v1/openings"
ACME_JOB_PAGES = re.compile(r"^https://acme\.hire\.trakstar\.com/jobs/")
# What the job pages of a closed account answer, with status 404.
INACTIVE_PAGE = (
    "<html><head><title>Trakstar Hire</title></head><body>"
    "<h1>Inactive account.</h1><p>This employer is no longer using Trakstar "
    "Hire to collect applications. Please contact the employer directly for "
    "information on how to apply.</p></body></html>"
)
UNVERIFIED_PAGE = INACTIVE_PAGE.replace("Inactive account.", "Unverified account.")


@pytest.fixture(autouse=True)
def _acme_is_an_open_account(httpx_mock) -> None:
    """Job pages of ``acme``, for the closed-account check."""
    httpx_mock.add_response(
        url=ACME_JOB_PAGES,
        html="<html><head><title>Senior Engineer at Acme</title></head></html>",
        is_optional=True,
        is_reusable=True,
    )


def _opening(
    *,
    oid: str = "abc123",
    title: str = "Senior Engineer",
    city: str | None = "Berlin",
    state: str | None = None,
    country: str | None = "Germany",
    allows_remote: bool = False,
    position_type: str = "full_time",
    team: str | None = "Platform",
    description: str | None = "<p>Build things.</p>",
    client: str = "acme",
) -> dict:
    return {
        "id": oid,
        "title": title,
        "client_name": client,
        "description": description,
        "location": {
            "city": city, "state": state, "country": country, "zipcode": None,
        },
        "tags": [],
        "hosted_url": f"https://{client}.hire.trakstar.com/jobs/{oid}/",
        "allows_remote": allows_remote,
        "position_type": position_type,
        "team": team,
        "close_date": None,
        "created_on": "2026-04-01T10:00:00Z",
    }


def _page(items: list[dict], offset: int = 0, total: int | None = None) -> dict:
    return {
        "meta": {
            "offset": offset,
            "limit": 100,
            "total": total if total is not None else offset + len(items),
        },
        "objects": items,
    }


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_recruiterbox() -> None:
    assert ScraperRegistry.get(ATSType.RECRUITERBOX) is RecruiterboxScraper


# --- Happy path -------------------------------------------------------------


def test_parses_basic_opening(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json=_page([_opening()]),
    )
    jobs = RecruiterboxScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "abc123"
    assert job.title == "Senior Engineer"
    assert job.location == "Berlin, Germany"
    assert job.is_remote is False
    assert job.employment_type == "FULL_TIME"
    assert job.team == "Platform"
    assert job.description and "Build things" in job.description
    assert job.url.unicode_string().startswith("https://acme.hire.trakstar.com/")
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_returns_empty_for_zero_total(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json={"meta": {"offset": 0, "limit": 100, "total": 0}, "objects": []},
    )
    assert RecruiterboxScraper("acme").fetch() == []


def test_paginates_until_total(httpx_mock) -> None:
    """Recruiterbox paginates server-side; we exhaust until we've seen
    ``total`` rows. With items per page < limit, pagination still terminates
    via the safety net."""
    page1 = _page(
        [_opening(oid=f"{i}") for i in range(100)],
        offset=0,
        total=180,
    )
    page2 = _page(
        [_opening(oid=f"{i}") for i in range(100, 180)],
        offset=100,
        total=180,
    )
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100", json=page1,
    )
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=100&limit=100", json=page2,
    )
    jobs = RecruiterboxScraper("acme").fetch()
    assert len(jobs) == 180


def test_dedupes_by_id_across_pages(httpx_mock) -> None:
    """If the server inadvertently returns the same id twice, we only
    surface it once."""
    page1 = _page([_opening(oid="X")], offset=0, total=2)
    page2 = _page([_opening(oid="X", title="dup")], offset=1, total=2)
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100", json=page1,
    )
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=1&limit=100", json=page2,
    )
    jobs = RecruiterboxScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["X"]


def test_skips_opening_without_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json=_page([
            {"id": "", "title": "Empty id", "hosted_url": "https://x"},
            {"id": "Y", "title": "", "hosted_url": "https://x"},
            _opening(oid="OK"),
        ]),
    )
    jobs = RecruiterboxScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


def test_remote_flag_propagates(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json=_page([_opening(oid="r", allows_remote=True)]),
    )
    assert RecruiterboxScraper("acme").fetch()[0].is_remote is True


# --- Errors ----------------------------------------------------------------


def test_400_invalid_client_name_raises_not_found(httpx_mock) -> None:
    """The API returns 400 with `{"client_name": "Invalid client name"}`
    for unknown tenants — we surface that as a CompanyNotFoundError."""
    httpx_mock.add_response(
        url=f"{API}?client_name=missing&offset=0&limit=100",
        status_code=400,
        json={"client_name": "Invalid client name"},
    )
    with pytest.raises(CompanyNotFoundError):
        RecruiterboxScraper("missing").fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch
    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 3)
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100", status_code=503,
    )
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json=_page([_opening()]),
    )
    assert len(RecruiterboxScraper("acme").fetch()) == 1


def test_malformed_json_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100", text="not json",
    )
    with pytest.raises(ScraperError, match="malformed JSON"):
        RecruiterboxScraper("acme").fetch()


# --- Closed accounts ---------------------------------------------------------


def _job_page_requests(httpx_mock) -> list[str]:
    return [
        str(request.url)
        for request in httpx_mock.get_requests()
        if request.url.host.endswith(".hire.trakstar.com")
    ]


@pytest.mark.parametrize("page", [INACTIVE_PAGE, UNVERIFIED_PAGE])
def test_closed_account_raises_not_found(httpx_mock, page: str) -> None:
    """The feed still lists the openings of an account the employer has
    closed; the job pages say so."""
    httpx_mock.add_response(
        url=f"{API}?client_name=gone&offset=0&limit=100",
        json=_page([_opening(oid="a", client="gone"), _opening(oid="b", client="gone")]),
    )
    httpx_mock.add_response(
        url="https://gone.hire.trakstar.com/jobs/a/", status_code=404, html=page
    )

    with pytest.raises(CompanyNotFoundError, match="gone is closed"):
        RecruiterboxScraper("gone").fetch()
    # One page is enough to tell.
    assert _job_page_requests(httpx_mock) == ["https://gone.hire.trakstar.com/jobs/a/"]


def test_open_account_reads_one_job_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json=_page([_opening(oid=str(n)) for n in range(5)]),
    )

    jobs = RecruiterboxScraper("acme").fetch()

    assert len(jobs) == 5
    request = next(
        r for r in httpx_mock.get_requests() if r.url.host == "acme.hire.trakstar.com"
    )
    assert str(request.url) == "https://acme.hire.trakstar.com/jobs/0/"
    assert request.headers["Accept"].startswith("text/html")
    assert len(_job_page_requests(httpx_mock)) == 1


def test_removed_posting_on_an_open_account_is_not_a_closed_account(httpx_mock) -> None:
    """A job page can answer 404 because that one posting was taken down."""
    httpx_mock.add_response(
        url=f"{API}?client_name=busy&offset=0&limit=100",
        json=_page([_opening(oid="a", client="busy"), _opening(oid="b", client="busy")]),
    )
    httpx_mock.add_response(
        url="https://busy.hire.trakstar.com/jobs/a/",
        status_code=404,
        html="<title>Busy jobs | Busy openings | Busy careers</title>",
    )

    jobs = RecruiterboxScraper("busy").fetch()

    assert [job.ats_id for job in jobs] == ["a", "b"]


def test_live_page_quoting_the_notice_is_not_a_closed_account(httpx_mock) -> None:
    """The notice counts only on a 404: a posting can contain the sentence."""
    httpx_mock.add_response(
        url=f"{API}?client_name=quoter&offset=0&limit=100",
        json=_page([_opening(oid="a", client="quoter")]),
    )
    httpx_mock.add_response(
        url="https://quoter.hire.trakstar.com/jobs/a/", status_code=200, html=INACTIVE_PAGE
    )

    jobs = RecruiterboxScraper("quoter").fetch()

    assert [job.ats_id for job in jobs] == ["a"]


def test_job_page_redirect_is_not_followed(httpx_mock) -> None:
    """The check stays on the Trakstar Hire host the feed named."""
    httpx_mock.add_response(
        url=f"{API}?client_name=mover&offset=0&limit=100",
        json=_page([_opening(oid="a", client="mover")]),
    )
    httpx_mock.add_response(
        url="https://mover.hire.trakstar.com/jobs/a/",
        status_code=302,
        headers={"Location": "https://elsewhere.example.com/gone"},
    )

    jobs = RecruiterboxScraper("mover").fetch()

    assert [job.ats_id for job in jobs] == ["a"]
    assert [r.url.host for r in httpx_mock.get_requests()] == [
        "jsapi.recruiterbox.com",
        "mover.hire.trakstar.com",
    ]


def test_job_page_that_cannot_be_read_keeps_the_openings(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 1)
    httpx_mock.add_response(
        url=f"{API}?client_name=flaky&offset=0&limit=100",
        json=_page([_opening(oid="a", client="flaky")]),
    )
    httpx_mock.add_response(
        url="https://flaky.hire.trakstar.com/jobs/a/", status_code=503
    )

    jobs = RecruiterboxScraper("flaky").fetch()

    assert [job.ats_id for job in jobs] == ["a"]


def test_account_without_openings_reads_no_job_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100",
        json={"meta": {"offset": 0, "limit": 100, "total": 0}, "objects": []},
    )

    assert RecruiterboxScraper("acme").fetch() == []
    assert _job_page_requests(httpx_mock) == []


def test_job_page_on_another_host_is_not_requested(httpx_mock) -> None:
    opening = _opening(oid="a")
    opening["hosted_url"] = "https://careers.example.com/jobs/a/"
    httpx_mock.add_response(
        url=f"{API}?client_name=acme&offset=0&limit=100", json=_page([opening])
    )

    jobs = RecruiterboxScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["a"]
    assert [str(r.url.host) for r in httpx_mock.get_requests()] == [
        "jsapi.recruiterbox.com"
    ]


def test_earlier_account_name_yields_the_current_one_as_company(httpx_mock) -> None:
    """An account asked for under an earlier name answers with its current
    name, so both names give the same ``(company, ats_id)``."""
    httpx_mock.add_response(
        url=f"{API}?client_name=oldname&offset=0&limit=100",
        json=_page([_opening(oid="a")]),
    )

    job = RecruiterboxScraper("oldname").fetch()[0]

    assert job.company == "acme"
    assert str(job.url) == "https://acme.hire.trakstar.com/jobs/a/"
