"""Tests for the Recruitee scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import RecruiteeScraper, ScraperRegistry

API = "https://acme.recruitee.com/api/offers"


def _offer(oid: int = 1, title: str = "Senior Engineer",
           city: str = "Berlin", country: str = "Germany") -> dict:
    return {
        "id": oid,
        "title": title,
        "city": city,
        "country": country,
        "country_code": "DE",
        "company_name": "AcmeCorp",
        "remote": False,
        "careers_url": f"https://acme.recruitee.com/o/{oid}",
        "created_at": "2026-04-15T08:00:00Z",
    }


def test_registry_resolves_recruitee() -> None:
    assert ScraperRegistry.get(ATSType.RECRUITEE) is RecruiteeScraper


def test_parses_basic_offer(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"offers": [_offer()]})
    jobs = RecruiteeScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Engineer"
    assert job.ats_type is ATSType.RECRUITEE


def test_returns_empty_offers(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"offers": []})
    assert RecruiteeScraper("acme").fetch() == []


def test_404_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        RecruiteeScraper("acme").fetch()


def test_full_url_slug(httpx_mock) -> None:
    """When the slug is a full URL we should hit it directly (custom domain
    support), still appending /api/offers if missing."""
    httpx_mock.add_response(
        url="https://careers.example.com/api/offers",
        json={"offers": [_offer()]},
    )
    jobs = RecruiteeScraper("https://careers.example.com").fetch()
    assert len(jobs) == 1


def test_full_url_slug_fallback_url_uses_custom_domain(httpx_mock) -> None:
    offer = _offer()
    offer.pop("careers_url")
    offer["slug"] = "senior-engineer"
    httpx_mock.add_response(
        url="https://careers.example.com/api/offers",
        json={"offers": [offer]},
    )

    jobs = RecruiteeScraper("https://careers.example.com").fetch()

    assert str(jobs[0].url) == "https://careers.example.com/o/senior-engineer"


def test_non_json_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API, text="<html>nope</html>")
    with pytest.raises(ScraperError):
        RecruiteeScraper("acme").fetch()


def test_country_iso_from_offer_country_code(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"offers": [_offer()]})
    assert RecruiteeScraper("acme").fetch()[0].country_iso == "DE"


@pytest.mark.parametrize(
    ("offer_code", "location_codes", "expected"),
    [
        ("NL", ["nl", "NL"], "NL"),
        ("NL", ["NL", None], "NL"),
        ("NL", ["NL", "DE"], None),
        (None, ["NL", "DE"], None),
        (None, ["nl", "NL"], "NL"),
        ("DE", [None], "DE"),
        ("USA", [], None),
        ("", [], None),
    ],
)
def test_country_iso_from_locations(httpx_mock, offer_code, location_codes, expected) -> None:
    """The offers API also lists places under ``locations``: the one code the
    entries share wins, differing codes leave ``country_iso`` unset, and the
    offer-level code is used only when no entry carries one."""
    offer = _offer()
    offer["country_code"] = offer_code
    offer["locations"] = [{"city": "X", "country_code": code} for code in location_codes]
    httpx_mock.add_response(url=API, json={"offers": [offer]})
    assert RecruiteeScraper("acme").fetch()[0].country_iso == expected
