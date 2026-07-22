"""Tests for the Recruiterbox / Trakstar Hire scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import RecruiterboxScraper, ScraperRegistry

API = "https://jsapi.recruiterbox.com/v1/openings"


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
) -> dict:
    return {
        "id": oid,
        "title": title,
        "client_name": "acme",
        "description": description,
        "location": {
            "city": city, "state": state, "country": country, "zipcode": None,
        },
        "tags": [],
        "hosted_url": f"https://acme.hire.trakstar.com/jobs/{oid}/",
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
