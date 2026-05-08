"""Tests for the Mercor scraper.

Mercor's old ``__NEXT_DATA__`` HTML scrape was broken by their CSR migration
in 2024. The current data source is the JSON API at:

    https://aws.api.mercor.com/work/listings-explore-page

Returns all listings in one call (no pagination). Each listing carries
title, company, location, rate, description inline.

These tests pin:

1. JSON API parsing (fields: listingId, title, companyName, etc.)
2. URL composition: ``work.mercor.com/jobs/{id}/{slug}``
3. Salary period mapping from ``payRateFrequency``
4. Employment type mapping from ``commitment``
5. Description truncation
6. Retry behaviour
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import MercorScraper, ScraperRegistry, get_scraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.mercor as mr
    monkeypatch.setattr(mr, "MAX_RETRIES", 1)
    monkeypatch.setattr(mr, "RETRY_BASE_DELAY", 0.0)


URL = "https://aws.api.mercor.com/work/listings-explore-page"


def _listing(
    *,
    listing_id: str = "list_ABC123",
    title: str = "Senior Engineer",
    company: str = "Acme Co",
    location: str = "Remote",
    rate_min: float | None = 80,
    rate_max: float | None = 100,
    pay_freq: str = "hourly",
    commitment: str = "full-time",
    description: str = "Build cool stuff.",
    posted_at: str = "2026-04-01T10:00:00Z",
    hoursPerWeek: int | None = None,  # noqa: N803  match Mercor's API casing
    workArrangement: str | None = None,  # noqa: N803
) -> dict:
    return {
        "listingId": listing_id,
        "title": title,
        "companyName": company,
        "location": location,
        "rateMin": rate_min,
        "rateMax": rate_max,
        "payRateFrequency": pay_freq,
        "commitment": commitment,
        "description": description,
        "postedAt": posted_at,
        "hoursPerWeek": hoursPerWeek,
        "workArrangement": workArrangement,
    }


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_mercor() -> None:
    assert ScraperRegistry.get(ATSType.MERCOR) is MercorScraper


def test_get_scraper_returns_mercor() -> None:
    s = get_scraper("mercor", "any")
    assert isinstance(s, MercorScraper)


# --- Happy path -------------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [_listing()]})
    jobs = MercorScraper("any").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "list_ABC123"
    assert job.title == "Senior Engineer"
    assert job.company == "Acme Co"
    assert job.location == "Remote"
    assert job.salary_min == 80
    assert job.salary_max == 100
    assert job.salary_currency == "USD"
    assert job.salary_period == "HOUR"
    assert job.ats_type is ATSType.MERCOR


def test_returns_empty_when_no_listings(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": []})
    assert MercorScraper("any").fetch() == []


def test_handles_missing_listings_key(httpx_mock) -> None:
    """Mercor occasionally returns ``{}`` on transient empty states; treat
    as no jobs rather than crash."""
    httpx_mock.add_response(url=URL, json={})
    assert MercorScraper("any").fetch() == []


def test_dedupes_listings_with_same_id(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(listing_id="X", title="Job 1"),
        _listing(listing_id="X", title="Job 1 dup"),
    ]})
    jobs = MercorScraper("any").fetch()
    assert len(jobs) == 1


def test_skips_listing_without_id_or_title(httpx_mock) -> None:
    """Defensive — Mercor's API has occasionally returned half-built rows
    when a listing was being created/deleted mid-fetch."""
    httpx_mock.add_response(url=URL, json={"listings": [
        {"listingId": "", "title": "No id"},
        {"listingId": "x", "title": ""},
        _listing(listing_id="OK", title="Real"),
    ]})
    jobs = MercorScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


# --- URL composition --------------------------------------------------------


def test_url_uses_work_mercor_host_with_slugified_title(httpx_mock) -> None:
    """The library returns a ``work.mercor.com/jobs/{id}/{slug}`` URL —
    that's the public-facing job page, not the API host."""
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(listing_id="list_42", title="Senior  AI / ML Engineer!"),
    ]})
    jobs = MercorScraper("any").fetch()
    assert str(jobs[0].url) == "https://work.mercor.com/jobs/list_42/senior-ai-ml-engineer"


def test_slugify_strips_unicode_and_punctuation(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(listing_id="X", title="C++ Developer (Senior)"),
    ]})
    jobs = MercorScraper("any").fetch()
    assert "(" not in str(jobs[0].url)
    assert "+" not in str(jobs[0].url)


# --- Salary period mapping --------------------------------------------------


@pytest.mark.parametrize(
    ("freq", "expected"),
    [
        ("hourly", "HOUR"),
        ("daily", "DAY"),
        ("weekly", "WEEK"),
        ("monthly", "MONTH"),
        ("yearly", "YEAR"),
        ("annually", "YEAR"),
        ("unknown-thing", None),  # unmapped — leave None, don't guess
        ("", None),
    ],
)
def test_salary_period_mapping(httpx_mock, freq: str, expected: str | None) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(pay_freq=freq, rate_min=50, rate_max=100),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].salary_period == expected


def test_salary_currency_only_set_when_rates_present(httpx_mock) -> None:
    """If a listing has no rate, we shouldn't claim USD — that would mislead
    consumers filtering by currency."""
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(rate_min=None, rate_max=None),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].salary_currency is None


# --- employment_type / commitment -------------------------------------------


@pytest.mark.parametrize(
    "commitment",
    ["hourly", "weekly", "monthly", "full-time", ""],
)
def test_employment_type_is_always_contract(
    httpx_mock, commitment: str,
) -> None:
    """Mercor is a contract talent marketplace — every listing is a
    contract role regardless of the rate frequency exposed in the
    API's ``commitment`` field. We default ``employment_type`` to
    ``CONTRACT`` and surface the rate-frequency label in
    ``commitment`` for display."""
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(commitment=commitment),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].employment_type == "CONTRACT"


def test_commitment_label_includes_hours_per_week(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(commitment="hourly", hoursPerWeek=40),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].commitment == "Hourly · 40h/week"


def test_commitment_label_without_hours(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(commitment="hourly"),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].commitment == "Hourly"


# --- Description -----------------------------------------------------------


def test_description_truncated_to_10kb(httpx_mock) -> None:
    huge = "Lorem ipsum dolor sit amet. " * 800  # ~22kB
    httpx_mock.add_response(url=URL, json={"listings": [
        _listing(description=huge),
    ]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].description is not None
    assert len(jobs[0].description) <= 10_000


def test_description_none_when_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, json={"listings": [_listing(description="")]})
    jobs = MercorScraper("any").fetch()
    assert jobs[0].description is None


# --- Auth header (regression: removing this header → 401) ------------------


def test_sends_required_auth_and_origin_headers(httpx_mock) -> None:
    """The Mercor API rejects requests missing the literal ``Authorization:
    Bearer`` (no token) plus origin/referer. If we ever drop these, every
    fetch will silently fail with 401."""
    httpx_mock.add_response(url=URL, json={"listings": [_listing()]})
    MercorScraper("any").fetch()
    request = httpx_mock.get_requests()[0]
    assert request.headers.get("Authorization") == "Bearer"
    assert request.headers.get("Origin") == "https://work.mercor.com"
    assert request.headers.get("Referer") == "https://work.mercor.com/"


# --- Error handling --------------------------------------------------------


def test_retries_on_5xx(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.mercor as mr
    monkeypatch.setattr(mr, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, json={"listings": [_listing()]})
    jobs = MercorScraper("any").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_is_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.mercor as mr
    monkeypatch.setattr(mr, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=URL, status_code=429, headers={"Retry-After": "13"}
    )
    httpx_mock.add_response(url=URL, json={"listings": [_listing()]})
    MercorScraper("any").fetch()
    assert 13.0 in sleeps


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.mercor as mr
    monkeypatch.setattr(mr, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=URL, status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        MercorScraper("any").fetch()


def test_network_error_raises(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.mercor as mr
    monkeypatch.setattr(mr, "MAX_RETRIES", 2)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=URL, is_reusable=True
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        MercorScraper("any").fetch()


def test_malformed_json_raises_clean_error(httpx_mock) -> None:
    """If Mercor's CDN ever responds with HTML (e.g. a maintenance page),
    surface a clean ScraperError, not a raw json.JSONDecodeError."""
    httpx_mock.add_response(url=URL, text="<html>maintenance</html>")
    with pytest.raises(ScraperError, match="malformed JSON"):
        MercorScraper("any").fetch()
