"""Tests for the Phenom scraper.

Phenom-powered career sites (Bell Canada, GE Healthcare, T-Mobile, etc.) all
share the same widget endpoint:

    POST {base_url}/widgets

with a CSRF token seeded by a prior GET to the search-results page. The
old jobhive ``GET /api/jobs`` was wrong for the vast majority of tenants.

These tests pin:

1. Construction (full URL required, locale + country defaults)
2. CSRF flow (cookie or page-embedded)
3. POST /widgets payload shape (the fields the API actually requires)
4. Concurrent fan-out via ``totalHits``
5. Response shape variations (``data.jobs``, ``hits``, top-level ``jobs``)
6. Retry behaviour
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import PhenomScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.phenom as ph
    monkeypatch.setattr(ph, "MAX_RETRIES", 1)
    monkeypatch.setattr(ph, "RETRY_BASE_DELAY", 0.0)


BASE = "https://jobs.acme.com"
SEARCH_URL = f"{BASE}/us/en/search-results"
WIDGETS_URL = f"{BASE}/widgets"


def _csrf_page() -> str:
    return '<html><script>window.config = {"csrfToken":"tok-abc-123"};</script></html>'


def _job(
    *,
    job_id: str = "100",
    title: str = "Engineer",
    city: str = "Berlin",
    state: str = "BE",
    country: str = "Germany",
    department: str = "Engineering",
    posted: str | None = "2026-04-01T10:00:00Z",
    description: str = "<p>Build it.</p>",
) -> dict[str, Any]:
    return {
        "jobId": job_id,
        "title": title,
        "city": city,
        "state": state,
        "country": country,
        "department": department,
        "dateCreated": posted,
        "descriptionTeaser": description,
    }


def _search_response(jobs: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {"refineSearch": {"data": {"jobs": jobs, "totalHits": total}}}


def _seed_csrf(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_URL, text=_csrf_page())


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_phenom() -> None:
    assert ScraperRegistry.get(ATSType.PHENOM) is PhenomScraper


# --- Construction -----------------------------------------------------------


def test_requires_full_url() -> None:
    with pytest.raises(ScraperError, match="full URL"):
        PhenomScraper("acme")


def test_default_locale_and_country_are_us() -> None:
    """``en_us`` / ``us`` covers the long tail of Phenom tenants. Tenants
    on other locales pass them at construction."""
    s = PhenomScraper("https://x.example.com")
    assert s.locale == "en_us"
    assert s.country == "us"


def test_locale_country_settable() -> None:
    s = PhenomScraper(
        "https://jobs.bell.ca", locale="en_ca", country="ca"
    )
    assert s.locale == "en_ca"
    assert s.country == "ca"


# --- CSRF flow --------------------------------------------------------------


def test_csrf_token_extracted_from_page(httpx_mock) -> None:
    """If the cookie path doesn't deliver, the token must come from the
    HTML body of the search-results page."""
    httpx_mock.add_response(url=SEARCH_URL, text=_csrf_page())
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([_job(job_id="A")], total=1),
    )
    PhenomScraper(BASE).fetch()
    request = httpx_mock.get_requests(url=WIDGETS_URL)[0]
    assert request.headers.get("x-csrf-token") == "tok-abc-123"


def test_csrf_token_extracted_from_cookies(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SEARCH_URL, text="<html></html>",
        headers={"set-cookie": "csrf-token=cookie-tok-456; Path=/"},
    )
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], total=1),
    )
    PhenomScraper(BASE).fetch()
    request = httpx_mock.get_requests(url=WIDGETS_URL)[0]
    assert request.headers.get("x-csrf-token") == "cookie-tok-456"


def test_csrf_missing_does_not_block_fetch(httpx_mock) -> None:
    """Some tenants don't issue a CSRF for anonymous searches. We try
    without the header; the request still succeeds."""
    httpx_mock.add_response(url=SEARCH_URL, text="<html></html>")
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], total=1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert len(jobs) == 1


# --- Search request payload -------------------------------------------------


def test_payload_includes_required_phenom_keys(httpx_mock) -> None:
    """The widget endpoint silently returns empty when ``ddoKey`` or
    ``pageName`` are missing — pin them as a contract."""
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], total=1),
    )
    PhenomScraper(BASE).fetch()
    request = httpx_mock.get_requests(url=WIDGETS_URL)[0]
    import json
    payload = json.loads(request.content)
    assert payload["ddoKey"] == "refineSearch"
    assert payload["pageName"] == "search-results"
    assert payload["jobs"] is True
    assert payload["counts"] is True
    assert payload["lang"] == "en_us"
    assert payload["country"] == "us"


def test_payload_uses_configured_locale_and_country(httpx_mock) -> None:
    bell_search = "https://jobs.bell.ca/ca/en/search-results"
    bell_widgets = "https://jobs.bell.ca/widgets"
    httpx_mock.add_response(url=bell_search, text=_csrf_page())
    httpx_mock.add_response(
        url=bell_widgets, json=_search_response([_job()], total=1),
    )
    PhenomScraper("https://jobs.bell.ca", locale="en_ca", country="ca").fetch()
    request = httpx_mock.get_requests(url=bell_widgets)[0]
    import json
    payload = json.loads(request.content)
    assert payload["lang"] == "en_ca"
    assert payload["country"] == "ca"


# --- Pagination via totalHits -----------------------------------------------


def test_paginates_concurrently_using_total_hits(httpx_mock) -> None:
    """First request returns total=250 + first 100. We must fan out
    additional requests at offsets 100 and 200."""
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([_job(job_id=str(i)) for i in range(100)], total=250),
        is_reusable=True,
    )
    # Concurrent fan-out — all subsequent calls hit WIDGETS_URL with
    # different `from` payloads. Reusable mock ack's all of them.
    jobs = PhenomScraper(BASE).fetch()
    # We get 100 unique IDs from the first page; the reused mock returns
    # the same IDs for offsets 100/200 → dedup keeps just 100.
    assert len({j.ats_id for j in jobs}) == 100


def test_no_pagination_when_total_le_first_page(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([_job(job_id=str(i)) for i in range(50)], total=50),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert len(jobs) == 50


def test_handles_missing_total_gracefully(httpx_mock) -> None:
    """Old tenants don't send ``totalHits``; treat as 'one page' and stop."""
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json={"refineSearch": {"data": {"jobs": [_job()]}}},
    )
    jobs = PhenomScraper(BASE).fetch()
    assert len(jobs) == 1


# --- Response shape variations ----------------------------------------------


def test_extracts_jobs_from_data_jobs_path(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json={"refineSearch": {"data": {"jobs": [_job(job_id="A")], "totalHits": 1}}},
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].ats_id == "A"


def test_extracts_jobs_from_refine_search_jobs_path(httpx_mock) -> None:
    """Older Phenom installs put jobs at ``refineSearch.jobs`` (no
    ``data`` wrapper)."""
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json={"refineSearch": {"jobs": [_job(job_id="OLD")], "totalHits": 1}},
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].ats_id == "OLD"


def test_extracts_jobs_from_top_level_jobs_path(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json={"jobs": [_job(job_id="TOP")]},
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].ats_id == "TOP"


# --- Field extraction -------------------------------------------------------


def test_location_combines_city_state_country(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([_job(city="Paris", state="IDF", country="France")], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].location == "Paris, IDF, France"


def test_location_falls_back_to_city_state_country_field(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([{
            "jobId": "X",
            "title": "X",
            "cityStateCountry": "Tokyo, Japan",
        }], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].location == "Tokyo, Japan"


def test_description_strips_html_and_truncates(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    huge = "<p>" + ("Lorem ipsum. " * 1000) + "</p>"
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([_job(description=huge)], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert jobs[0].description is not None
    assert "<p>" not in jobs[0].description
    assert len(jobs[0].description) <= 25_000


def test_url_is_full_when_relative(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([{
            "jobId": "X", "title": "Eng", "jobUrl": "/job/X",
        }], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert str(jobs[0].url) == f"{BASE}/job/X"


def test_url_is_synthesized_when_missing(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([{"jobId": "X", "title": "Eng"}], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert str(jobs[0].url) == f"{BASE}/job/X"


# --- Error handling --------------------------------------------------------


def test_init_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        PhenomScraper(BASE).fetch()


def test_widgets_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.phenom as ph
    monkeypatch.setattr(ph, "MAX_RETRIES", 3)
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(url=WIDGETS_URL, status_code=503)
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert len(jobs) == 1


def test_widgets_429_with_retry_after_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.phenom as ph
    monkeypatch.setattr(ph, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    _seed_csrf(httpx_mock)
    httpx_mock.add_response(
        url=WIDGETS_URL, status_code=429, headers={"Retry-After": "14"}
    )
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], 1),
    )
    PhenomScraper(BASE).fetch()
    assert 14.0 in sleeps


def test_malformed_json_raises_clean_error(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(url=WIDGETS_URL, text="<html>nope</html>")
    with pytest.raises(ScraperError, match="malformed JSON"):
        PhenomScraper(BASE).fetch()


def test_network_error_raises(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.phenom as ph
    monkeypatch.setattr(ph, "MAX_RETRIES", 2)
    _seed_csrf(httpx_mock)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=WIDGETS_URL, is_reusable=True
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        PhenomScraper(BASE).fetch()
