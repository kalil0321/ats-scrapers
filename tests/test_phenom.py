"""Tests for the Phenom scraper.

Phenom-powered career sites (Bell Canada, GE Healthcare, T-Mobile, etc.) all
share the same widget endpoint:

    POST {base_url}/widgets

with a CSRF token seeded by a prior GET to the search-results page. The
old ``GET /api/jobs`` implementation was wrong for the vast majority of tenants.

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
import json
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import PhenomScraper, ScraperRegistry, phenom

# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture in
# conftest.py — the shared fetch layer replaced per-scraper retry constants.

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
    assert str(jobs[0].url) == f"{BASE}/us/en/job/X"


def test_synthesized_url_uses_configured_country_and_language(httpx_mock) -> None:
    bell = "https://jobs.bell.ca"
    httpx_mock.add_response(url=f"{bell}/ca/fr/search-results", text=_csrf_page())
    httpx_mock.add_response(
        url=f"{bell}/widgets",
        json=_search_response([{"jobId": "X", "title": "Eng"}], 1),
    )
    jobs = PhenomScraper(bell, locale="fr_ca", country="ca").fetch()
    assert str(jobs[0].url) == f"{bell}/ca/fr/job/X"


def test_synthesized_url_follows_the_path_the_site_reports(httpx_mock) -> None:
    page = (
        '<html><script>window.config = {"csrfToken":"tok",'
        f'"baseUrl":"{BASE}/global/en/"' + "};</script></html>"
    )
    httpx_mock.add_response(url=SEARCH_URL, text=page)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([{"jobId": "X", "title": "Eng"}], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert str(jobs[0].url) == f"{BASE}/global/en/job/X"


def test_synthesized_url_ignores_a_reported_path_on_another_host(httpx_mock) -> None:
    page = (
        '<html><script>window.config = {"csrfToken":"tok",'
        '"baseUrl":"https://elsewhere.example/global/en/"};</script></html>'
    )
    httpx_mock.add_response(url=SEARCH_URL, text=page)
    httpx_mock.add_response(
        url=WIDGETS_URL,
        json=_search_response([{"jobId": "X", "title": "Eng"}], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert str(jobs[0].url) == f"{BASE}/us/en/job/X"


# --- Error handling --------------------------------------------------------


def test_init_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=SEARCH_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        PhenomScraper(BASE).fetch()


@pytest.mark.parametrize(
    "target",
    ["https://www.acme.com/careers/home", f"{BASE}/overview/"],
)
def test_redirect_off_phenom_is_not_found(httpx_mock, target: str) -> None:
    httpx_mock.add_response(
        url=SEARCH_URL, status_code=301, headers={"Location": target}
    )
    httpx_mock.add_response(
        url=target, text="<html><body>Careers at Acme</body></html>"
    )
    with pytest.raises(CompanyNotFoundError, match="redirects"):
        PhenomScraper(BASE).fetch()


def test_redirect_to_another_phenom_page_is_followed(httpx_mock) -> None:
    home = f"{BASE}/global/en"
    httpx_mock.add_response(
        url=SEARCH_URL, status_code=303, headers={"Location": home}
    )
    httpx_mock.add_response(
        url=home,
        text=(
            '<html><script>var phApp = {"csrfToken":"tok-abc-123"};'
            "</script></html>"
        ),
    )
    httpx_mock.add_response(url=WIDGETS_URL, json=_search_response([_job()], 1))
    assert len(PhenomScraper(BASE).fetch()) == 1


def test_blocked_search_page_is_not_mistaken_for_a_move(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SEARCH_URL,
        text="<html><body><h4>This Website is not available in your country"
        "</h4></body></html>",
    )
    httpx_mock.add_response(url=WIDGETS_URL, text="<html>blocked</html>")
    with pytest.raises(ScraperError, match="malformed JSON") as raised:
        PhenomScraper(BASE).fetch()
    assert not isinstance(raised.value, CompanyNotFoundError)


def test_unresolvable_site_is_not_found(httpx_mock) -> None:
    import socket

    try:
        raise httpx.ConnectError("connect failed") from socket.gaierror(
            socket.EAI_NONAME, "Name or service not known"
        )
    except httpx.ConnectError as exc:
        httpx_mock.add_exception(exc, url=SEARCH_URL, is_reusable=True)
    with pytest.raises(CompanyNotFoundError, match="does not resolve"):
        PhenomScraper(BASE).fetch()


def test_requests_name_the_client(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(url=WIDGETS_URL, json=_search_response([_job()], 1))
    PhenomScraper(BASE).fetch()
    agents = {request.headers["User-Agent"] for request in httpx_mock.get_requests()}
    assert agents == {"Mozilla/5.0 (compatible; ats-scrapers/1.0)"}


def test_widgets_5xx_retries(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_response(url=WIDGETS_URL, status_code=503)
    httpx_mock.add_response(
        url=WIDGETS_URL, json=_search_response([_job()], 1),
    )
    jobs = PhenomScraper(BASE).fetch()
    assert len(jobs) == 1


def test_widgets_429_with_retry_after_honored(monkeypatch, httpx_mock) -> None:
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


def test_network_error_raises(httpx_mock) -> None:
    _seed_csrf(httpx_mock)
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=WIDGETS_URL, is_reusable=True
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        PhenomScraper(BASE).fetch()


# --- Result window ----------------------------------------------------------


def _phenom_index(httpx_mock, monkeypatch, jobs: list[dict[str, Any]], window: int) -> list[dict]:
    """Serve ``jobs`` like Phenom: facet filters, aggregations, and no rows
    past ``window``. Returns the request payloads it received."""
    monkeypatch.setattr(phenom, "PAGE_SIZE", 2)
    monkeypatch.setattr(phenom, "RESULT_WINDOW", window)
    seen: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        hits = [
            job for job in jobs
            if all(job.get(field) in values for field, values in body["selected_fields"].items())
        ]
        start, size = body["from"], body["size"]
        page = hits[start:start + size] if start + size <= window else []
        aggregations = [
            {"field": field, "value": {
                value: sum(1 for job in hits if job.get(field) == value)
                for value in sorted({job[field] for job in hits if job.get(field)})
            }}
            for field in ("category", "state", "city")
        ]
        return httpx.Response(200, json={"refineSearch": {"data": {
            "jobs": page, "totalHits": len(hits), "aggregations": aggregations,
        }}})

    _seed_csrf(httpx_mock)
    httpx_mock.add_callback(reply, url=WIDGETS_URL, method="POST", is_reusable=True)
    return seen


def test_search_past_the_result_window_is_split_by_facet(httpx_mock, monkeypatch) -> None:
    jobs = [
        {"jobId": str(i), "title": "T", "category": "Stores" if i < 9 else "Office",
         "state": ["TX", "OH", "CA"][i % 3]}
        for i in range(11)
    ]
    seen = _phenom_index(httpx_mock, monkeypatch, jobs, window=4)

    found = PhenomScraper(BASE).fetch()

    assert sorted(int(j.ats_id) for j in found) == list(range(11))
    # "Stores" (9 jobs) is still too large, so it is split again by state.
    assert {"category": ["Stores"], "state": ["TX"]} in [b["selected_fields"] for b in seen]


def test_facet_that_leaves_jobs_out_is_not_used_to_split(httpx_mock, monkeypatch) -> None:
    jobs = [
        {"jobId": str(i), "title": "T", "category": "Stores" if i < 3 else None,
         "state": ["TX", "OH", "CA"][i % 3]}
        for i in range(9)
    ]
    seen = _phenom_index(httpx_mock, monkeypatch, jobs, window=4)

    found = PhenomScraper(BASE).fetch()

    assert len(found) == 9
    assert all("category" not in b["selected_fields"] for b in seen)


def test_window_is_read_and_shortfall_logged_when_no_facet_splits(
    httpx_mock, monkeypatch, caplog
) -> None:
    jobs = [{"jobId": str(i), "title": "T", "state": "TX"} for i in range(7)]
    _phenom_index(httpx_mock, monkeypatch, jobs, window=4)

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.phenom"):
        found = PhenomScraper(BASE).fetch()

    assert len(found) == 4
    assert "announced 7 jobs, collected 4" in caplog.text


def test_explicit_job_type_is_mapped():
    job = PhenomScraper(BASE)._parse_job({**_job(), "jobType": "Permanent"})
    assert job.engagement_type == "permanent" and job.commitment == "Permanent"
