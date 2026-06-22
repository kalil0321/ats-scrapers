"""Tests for the Pinpoint scraper."""

from __future__ import annotations

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import PinpointScraper, ScraperRegistry

URL = "https://acme.pinpointhq.com/postings.json"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.pinpoint as pp
    monkeypatch.setattr(pp, "MAX_RETRIES", 1)
    monkeypatch.setattr(pp, "RETRY_BASE_DELAY", 0.0)


def _posting(
    *,
    pid: str = "p1",
    title: str = "Senior Engineer",
    workplace: str = "remote",
    employment: str = "full_time",
    comp_visible: bool = False,
    comp_min: int | None = 100_000,
    comp_max: int | None = 150_000,
    comp_currency: str = "USD",
    comp_period: str = "yearly",
    department: str = "Engineering",
    location_name: str = "Remote",
) -> dict:
    return {
        "id": pid,
        "title": title,
        "url": f"https://acme.pinpointhq.com/en/postings/{pid}",
        "workplace_type": workplace,
        "employment_type": employment,
        "compensation_visible": comp_visible,
        "compensation_minimum": comp_min,
        "compensation_maximum": comp_max,
        "compensation_currency": comp_currency,
        "compensation_frequency": comp_period,
        "description": "<p>Build great <strong>things</strong>.</p>",
        "first_published_at": "2026-04-15T08:00:00Z",
        "job": {"department": {"id": "1", "name": department}},
        "location": {"city": "London", "name": location_name, "province": "London"},
    }


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_pinpoint() -> None:
    assert ScraperRegistry.get(ATSType.PINPOINT) is PinpointScraper


# --- Happy path -------------------------------------------------------------


def test_parses_basic_posting(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"data": [_posting(comp_visible=True)]})
    jobs = PinpointScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "p1"
    assert job.title == "Senior Engineer"
    assert job.location == "Remote"
    assert job.is_remote is True
    assert job.department == "Engineering"
    assert job.employment_type == "FULL_TIME"
    assert job.salary_min == 100_000
    assert job.salary_max == 150_000
    assert job.salary_currency == "USD"
    assert job.salary_period == "YEAR"
    assert job.description and "Build great" in job.description
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_returns_empty_for_empty_data(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"data": []})
    assert PinpointScraper("acme").fetch() == []


def test_dedupes_by_id(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        json={"data": [_posting(pid="X"), _posting(pid="X", title="dup")]},
    )
    assert len(PinpointScraper("acme").fetch()) == 1


def test_skips_posting_without_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        json={"data": [
            {"id": "", "title": "No id", "url": "https://x"},
            {"id": "Y", "title": "", "url": "https://x"},
            _posting(pid="OK"),
        ]},
    )
    jobs = PinpointScraper("acme").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


# --- Field extraction -------------------------------------------------------


def test_compensation_hidden_when_not_visible(httpx_mock) -> None:
    """Pinpoint surfaces compensation only when explicitly marked visible —
    we must respect that flag and NOT leak internal band data."""
    httpx_mock.add_response(
        url=URL,
        json={"data": [_posting(comp_visible=False)]},
    )
    job = PinpointScraper("acme").fetch()[0]
    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_period is None


def test_workplace_type_remote_maps_to_is_remote(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        json={"data": [
            _posting(pid="r", workplace="remote"),
            _posting(pid="o", workplace="onsite"),
            _posting(pid="h", workplace="hybrid"),
        ]},
    )
    jobs = PinpointScraper("acme").fetch()
    by_id = {j.ats_id: j for j in jobs}
    assert by_id["r"].is_remote is True
    assert by_id["o"].is_remote is False
    assert by_id["h"].is_remote is None  # hybrid: ambiguous


def test_html_description_preserved(httpx_mock) -> None:
    """Description now keeps HTML tags intact for downstream
    markdownification; only entities are decoded at scrape time."""
    p = _posting()
    p["description"] = "<div><p>Hello&nbsp;<b>world</b></p></div>"
    httpx_mock.add_response(url=URL, json={"data": [p]})
    job = PinpointScraper("acme").fetch()[0]
    assert "<b>world</b>" in job.description
    assert "&nbsp;" not in job.description  # entity decoded


def test_location_falls_back_to_city_when_name_missing(httpx_mock) -> None:
    p = _posting(location_name="")
    p["location"]["name"] = ""
    httpx_mock.add_response(url=URL, json={"data": [p]})
    jobs = PinpointScraper("acme").fetch()
    assert jobs[0].location == "London, London"


# --- Errors ----------------------------------------------------------------


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url="https://missing.pinpointhq.com/postings.json",
                            status_code=404)
    with pytest.raises(CompanyNotFoundError):
        PinpointScraper("missing").fetch()


def test_redirect_treated_as_not_found(httpx_mock) -> None:
    """Tenants without an active careers site sometimes redirect."""
    httpx_mock.add_response(
        url="https://inactive.pinpointhq.com/postings.json",
        status_code=302,
        headers={"Location": "https://www.pinpointhq.com/"},
    )
    with pytest.raises(CompanyNotFoundError):
        PinpointScraper("inactive").fetch()


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.pinpoint as pp
    monkeypatch.setattr(pp, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, json={"data": [_posting()]})
    assert len(PinpointScraper("acme").fetch()) == 1


def test_unexpected_payload_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"err": "x"})
    with pytest.raises(ScraperError, match="unexpected payload"):
        PinpointScraper("acme").fetch()


def test_malformed_json_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text="not json")
    with pytest.raises(ScraperError, match="malformed JSON"):
        PinpointScraper("acme").fetch()
