"""Tests for the BreezyHR scraper."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import BreezyScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch, httpx_mock) -> None:
    import jobhive.scrapers.breezy as br
    monkeypatch.setattr(br, "MAX_RETRIES", 1)
    monkeypatch.setattr(br, "RETRY_BASE_DELAY", 0.0)
    httpx_mock.add_response(
        url=re.compile(r"^https://acme\.breezy\.hr/p/"),
        text="<html><body><div class='description'>Build useful products.</div></body></html>",
        is_reusable=True,
        is_optional=True,
    )


URL = "https://acme.breezy.hr/json"


def _position(
    *,
    pos_id: str = "abc123",
    name: str = "Senior Engineer",
    location_name: str = "Berlin, Germany",
    is_remote: bool = False,
    department: str = "Engineering",
    salary: str = "$100k - $150k",
    type_id: str = "fullTime",
    company_name: str = "Acme",
) -> dict:
    return {
        "id": pos_id,
        "name": name,
        "url": f"https://acme.breezy.hr/p/{pos_id}",
        "published_date": "2026-04-01T10:00:00Z",
        "type": {"id": type_id, "name": "Full-Time"},
        "location": {
            "name": location_name,
            "city": "Berlin",
            "country": {"name": "Germany", "id": "DE"},
            "is_remote": is_remote,
        },
        "department": department,
        "salary": salary,
        "company": {"name": company_name},
    }


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_breezy() -> None:
    assert ScraperRegistry.get(ATSType.BREEZY) is BreezyScraper


# --- Happy path -------------------------------------------------------------


def test_parses_basic_position(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json=[_position()])
    jobs = BreezyScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "abc123"
    assert job.title == "Senior Engineer"
    assert job.location == "Berlin, Germany"
    assert job.company == "Acme"
    assert job.ats_type is ATSType.BREEZY
    assert job.is_remote is False
    assert job.department == "Engineering"
    assert job.salary_summary == "$100k - $150k"
    assert job.employment_type == "FULL_TIME"
    assert job.description == "Build useful products."
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_returns_empty_for_empty_array(httpx_mock) -> None:
    """Tenant has a Breezy site but no open positions."""
    httpx_mock.add_response(url=URL, json=[])
    assert BreezyScraper("acme").fetch() == []


def test_dedupes_by_id(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json=[
        _position(pos_id="X"), _position(pos_id="X", name="dup"),
    ])
    jobs = BreezyScraper("acme").fetch()
    assert len(jobs) == 1


def test_skips_position_without_id_or_name(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json=[
        {"id": "", "name": "No id", "url": "https://x"},
        {"id": "Y", "name": "", "url": "https://x"},
        _position(pos_id="OK"),
    ])
    jobs = BreezyScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


# --- Field extraction -------------------------------------------------------


def test_remote_flag_propagates(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json=[_position(is_remote=True)])
    jobs = BreezyScraper("acme").fetch()
    assert jobs[0].is_remote is True


def test_location_falls_back_to_city_country_when_name_missing(httpx_mock) -> None:
    """Some tenants don't pre-build the ``location.name`` — compose from
    structured city/country fields."""
    p = _position()
    del p["location"]["name"]
    httpx_mock.add_response(url=URL, json=[p])
    jobs = BreezyScraper("acme").fetch()
    assert jobs[0].location == "Berlin, Germany"


@pytest.mark.parametrize(
    ("type_id", "expected"),
    [
        ("fullTime", "FULL_TIME"),
        ("partTime", "PART_TIME"),
        ("contract", "CONTRACT"),
        ("internship", "INTERN"),
        ("intern", "INTERN"),
        ("temporary", "TEMPORARY"),
        ("freelance", None),  # unmapped
    ],
)
def test_employment_type_mapping(httpx_mock, type_id: str, expected: str | None) -> None:
    httpx_mock.add_response(url=URL, json=[_position(type_id=type_id)])
    jobs = BreezyScraper("acme").fetch()
    assert jobs[0].employment_type == expected


def test_company_name_falls_back_to_slug(httpx_mock) -> None:
    p = _position()
    del p["company"]
    httpx_mock.add_response(url=URL, json=[p])
    jobs = BreezyScraper("acme").fetch()
    assert jobs[0].company == "acme"


# --- 302 redirect = no active careers site ----------------------------------


def test_302_redirect_raises_company_not_found(httpx_mock) -> None:
    """Tenants without an active Breezy careers site are 302'd to the
    marketing page. We treat that as ``CompanyNotFoundError`` rather than
    return an empty list."""
    httpx_mock.add_response(
        url="https://inactive.breezy.hr/json",
        status_code=302,
        headers={"Location": "https://breezy.hr/"},
    )
    with pytest.raises(CompanyNotFoundError, match="no active careers site"):
        BreezyScraper("inactive").fetch()


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url="https://missing.breezy.hr/json", status_code=404)
    with pytest.raises(CompanyNotFoundError):
        BreezyScraper("missing").fetch()


# --- Retry / errors --------------------------------------------------------


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.breezy as br
    monkeypatch.setattr(br, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, json=[_position()])
    jobs = BreezyScraper("acme").fetch()
    assert len(jobs) == 1


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.breezy as br
    monkeypatch.setattr(br, "MAX_RETRIES", 2)
    httpx_mock.add_response(url=URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        BreezyScraper("acme").fetch()


def test_malformed_json_raises_clean_error(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text="not json")
    with pytest.raises(ScraperError, match="malformed JSON"):
        BreezyScraper("acme").fetch()


def test_non_list_response_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"error": "auth required"})
    with pytest.raises(ScraperError, match="non-list JSON"):
        BreezyScraper("acme").fetch()
