"""Tests for the The Hub (Nordic startup jobs) scraper.

Pin parsing of the rich The Hub payload — including geoLocation
GeoJSON-style ``[lon, lat]`` coordinates (must swap to lat-first
for our model) and the ``link`` field repurposed as ``apply_url``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, TheHubScraper

_API_RE = re.compile(r"^https://thehub\.io/api/jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.thehub as t
    monkeypatch.setattr(t, "MAX_RETRIES", 1)
    monkeypatch.setattr(t, "RETRY_BASE_DELAY", 0.0)


def _doc(
    *,
    job_id: str = "abc123",
    title: str = "Frontend Engineer",
    company: str = "Acme",
    address: str = "Frederiksberg, Denmark",
    locality: str = "Frederiksberg",
    country: str = "Denmark",
    country_code: str = "DK",
    is_remote: bool = False,
    apply_url: str = "https://apply.workable.com/acme/j/ABC/",
    salary_range: dict[str, Any] | None = None,
    salary_label: str = "competitive",
    status: str = "ACTIVE",
    coords: list[float] | None = None,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "company": {"name": company, "id": "co1"},
        "location": {"address": address, "locality": locality, "country": country},
        "countryCode": country_code,
        "isRemote": is_remote,
        "link": apply_url,
        "salary": salary_label,
        "salaryRange": salary_range or {},
        "status": status,
        "publishedAt": "2026-04-08T10:43:28.000Z",
        "geoLocation": {
            "center": {"type": "Point", "coordinates": coords or [12.513321, 55.677069]},
        },
        "description": "<p>Build things.</p>",
        "equity": "undisclosed",
        "jobRoles": ["role-1"],
        "jobPositionTypes": ["full-time"],
    }


def _envelope(docs: list[dict], pages: int = 1, total: int | None = None) -> dict:
    return {
        "docs": docs,
        "total": total if total is not None else len(docs),
        "limit": 15,
        "page": 1,
        "pages": pages,
    }


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_thehub() -> None:
    assert ScraperRegistry.get(ATSType.THEHUB) is TheHubScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_doc_with_geo_swap(httpx_mock) -> None:
    """The Hub ships ``geoLocation.center.coordinates`` as GeoJSON
    ``[lon, lat]``; our model uses lat-first. The scraper must swap."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([_doc()]))
    j = TheHubScraper("any").fetch()[0]
    assert j.ats_type is ATSType.THEHUB
    assert j.ats_id == "abc123"
    assert j.title == "Frontend Engineer"
    assert j.company == "Acme"
    assert j.location == "Frederiksberg, Denmark"
    # GeoJSON [12.513, 55.677] → our model: lat=55.677, lon=12.513
    assert j.lat == 55.677069
    assert j.lon == 12.513321
    assert j.is_remote is False
    assert str(j.apply_url) == "https://apply.workable.com/acme/j/ABC/"
    assert j.posted_at is not None
    assert j.description == "Build things."
    assert str(j.url) == "https://thehub.io/jobs/abc123"
    assert j.raw is not None
    assert j.raw["country_code"] == "DK"


# --- status filter ----------------------------------------------------------


def test_drops_non_active_postings(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="active", status="ACTIVE"),
        _doc(job_id="expired", status="EXPIRED"),
        _doc(job_id="draft", status="DRAFT"),
    ]))
    jobs = TheHubScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["active"]


# --- location fallback -----------------------------------------------------


def test_location_falls_back_to_locality_country_when_no_address(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="x", address="", locality="Stockholm", country="Sweden"),
    ]))
    assert TheHubScraper("any").fetch()[0].location == "Stockholm, Sweden"


# --- salary parsing ---------------------------------------------------------


def test_no_salary_when_only_label_is_competitive(httpx_mock) -> None:
    """The free-text ``salary`` field ('competitive', 'undisclosed') is
    not numeric — don't synthesize salary fields from it."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="x", salary_label="competitive", salary_range={}),
    ]))
    j = TheHubScraper("any").fetch()[0]
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_max is None


def test_salary_range_with_structured_object(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="x", salary_range={"from": 60000, "to": 90000, "currency": "EUR"}),
    ]))
    j = TheHubScraper("any").fetch()[0]
    assert j.salary_min == 60000
    assert j.salary_max == 90000
    assert j.salary_currency == "EUR"


# --- pagination -------------------------------------------------------------


def test_paginates_until_pages_count(httpx_mock) -> None:
    """Page 1 carries ``pages`` count; the scraper fans out the
    remaining N-1 pages in parallel."""
    # Probe (page=1) → pages=3
    httpx_mock.add_response(
        url="https://thehub.io/api/jobs?page=1",
        json=_envelope([_doc(job_id=f"a{i}") for i in range(15)], pages=3),
    )
    httpx_mock.add_response(
        url="https://thehub.io/api/jobs?page=2",
        json=_envelope([_doc(job_id=f"b{i}") for i in range(15)], pages=3),
    )
    httpx_mock.add_response(
        url="https://thehub.io/api/jobs?page=3",
        json=_envelope([_doc(job_id=f"c{i}") for i in range(10)], pages=3),
    )
    jobs = TheHubScraper("any").fetch()
    assert len(jobs) == 40  # 15 + 15 + 10 distinct ats_ids


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even if envelope says 1000 pages, ``max_pages=2`` must stop after 2
    page requests (probe + 1 fan-out)."""
    httpx_mock.add_response(
        url="https://thehub.io/api/jobs?page=1",
        json=_envelope([_doc(job_id=f"a{i}") for i in range(15)], pages=1000),
    )
    httpx_mock.add_response(
        url="https://thehub.io/api/jobs?page=2",
        json=_envelope([_doc(job_id=f"b{i}") for i in range(15)], pages=1000),
    )
    # Page 3 must NOT be requested — httpx_mock will error if it is.
    jobs = TheHubScraper("any", max_pages=2).fetch()
    assert len(jobs) == 30


def test_no_fanout_when_one_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([_doc(job_id="solo")], pages=1))
    assert len(TheHubScraper("any").fetch()) == 1


# --- defensive --------------------------------------------------------------


def test_empty_link_drops_apply_url_not_whole_job(httpx_mock) -> None:
    """Some live The Hub postings ship ``link=''`` — the Pydantic
    ``HttpUrl`` validator on Job.apply_url rejects empty strings, so
    blindly passing the field crashes the whole scrape. Regression
    for the live-verify ValidationError."""
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="empty-link", apply_url=""),
        _doc(job_id="weird-link", apply_url="javascript:void(0)"),
    ]))
    jobs = TheHubScraper("any").fetch()
    assert len(jobs) == 2
    assert all(j.apply_url is None for j in jobs)


def test_drops_doc_missing_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, json=_envelope([
        _doc(job_id="ok"),
        {"title": "no id", "status": "ACTIVE", "company": {"name": "X"},
         "location": {}, "geoLocation": {}},
        {"id": "no title", "status": "ACTIVE", "company": {"name": "X"},
         "location": {}, "geoLocation": {}},
    ]))
    jobs = TheHubScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["ok"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        TheHubScraper("any").fetch()
