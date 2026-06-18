"""Tests for the Cornerstone OnDemand scraper.

Cornerstone is a 2-step flow: extract a JWT token from the career-site HTML,
then POST to the regional ``api.csod.com`` host with the token. These tests
pin token extraction, region detection, pagination, and field parsing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import CornerstoneScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.cornerstone as cs
    monkeypatch.setattr(cs, "MAX_RETRIES", 1)
    monkeypatch.setattr(cs, "RETRY_BASE_DELAY", 0.0)


CAREER_URL = "https://acme.csod.com/ux/ats/careersite/1/home?c=acme"
API_URL = "https://eu-fra.api.csod.com/rec-job-search/external/jobs"


def _site_html(token: str = "tok-abc-123", api: str = "https://eu-fra.api.csod.com") -> str:
    return (
        f'<html><body>'
        f'<script>csod.context.token = "{token}"; var apiHost = "{api}";</script>'
        f'</body></html>'
    )


def _req(*, req_id: str = "100", title: str = "Engineer", city: str = "Berlin") -> dict:
    return {
        "requisitionId": req_id,
        "displayJobTitle": title,
        "locations": [{"city": city, "state": "BE", "country": "Germany"}],
        "externalDescription": "<p>Build it.</p>",
        "postingEffectiveDate": "2026-04-01T10:00:00Z",
    }


def _search_response(reqs: list[dict], total: int) -> dict:
    return {"data": {"requisitions": reqs, "totalCount": total}, "status": 200}


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_cornerstone() -> None:
    assert ScraperRegistry.get(ATSType.CORNERSTONE) is CornerstoneScraper


# --- Construction -----------------------------------------------------------


def test_default_career_url_built_from_slug() -> None:
    s = CornerstoneScraper("acme")
    assert s.career_url == "https://acme.csod.com/ux/ats/careersite/1/home?c=acme"
    assert s.slug == "acme"
    assert s.company_name == "acme"


def test_full_url_accepted() -> None:
    s = CornerstoneScraper("https://thekids.csod.com/ux/ats/careersite/4/home?c=thekids")
    assert s.career_url.startswith("https://thekids.csod.com")
    assert s.slug == "thekids"
    assert s.site_id == 4


def test_full_url_site_id_used_in_api_request(httpx_mock) -> None:
    career_url = "https://thekids.csod.com/ux/ats/careersite/4/home?c=thekids"
    httpx_mock.add_response(url=career_url, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))

    jobs = CornerstoneScraper(career_url).fetch()

    request = httpx_mock.get_requests(url=API_URL)[0]
    body = json.loads(request.content)
    assert body["careerSiteId"] == 4
    assert body["careerSitePageId"] == 4
    assert str(jobs[0].url) == (
        "https://thekids.csod.com/ux/ats/careersite/4/job/100?c=thekids"
    )


def test_custom_site_id() -> None:
    s = CornerstoneScraper("acme", site_id=4)
    assert "/careersite/4/" in s.career_url


# --- Token extraction -------------------------------------------------------


def test_extracts_token_and_api_host(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    request = httpx_mock.get_requests(url=API_URL)[0]
    assert request.headers["Authorization"] == "Bearer tok-abc-123"
    assert len(jobs) == 1


def test_falls_back_to_na_api_when_host_not_found(httpx_mock) -> None:
    """Most career-site pages embed the regional host; if the regex misses
    it, default to ``na.api.csod.com``."""
    httpx_mock.add_response(
        url=CAREER_URL,
        text='<html><script>csod.context.token = "x";</script></html>',
    )
    httpx_mock.add_response(
        url="https://na.api.csod.com/rec-job-search/external/jobs",
        json=_search_response([_req()], total=1),
    )
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_init_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        CornerstoneScraper("acme").fetch()


def test_missing_token_raises_helpful_error(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text="<html><body>no token</body></html>")
    with pytest.raises(ScraperError, match="couldn't extract JWT token"):
        CornerstoneScraper("acme").fetch()


# --- Search response parsing ------------------------------------------------


def test_parses_basic_requisition(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    job = jobs[0]
    assert job.ats_id == "100"
    assert job.title == "Engineer"
    assert job.location == "Berlin, BE, Germany"
    assert job.company == "acme"
    assert job.ats_type is ATSType.CORNERSTONE
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_company_name_override_is_used_for_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme", company_name="Acme Corp").fetch()
    assert jobs[0].company == "Acme Corp"


def test_strips_html_from_description(httpx_mock) -> None:
    req = _req()
    req["externalDescription"] = "<p>Hello <b>world</b>.</p>"
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([req], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert jobs[0].description == "Hello world ."


def test_dedupes_requisitions(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL,
        json=_search_response([_req(req_id="A"), _req(req_id="A", title="dup")], total=2),
    )
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_url_uses_career_origin(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req(req_id="X")], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert str(jobs[0].url) == (
        "https://acme.csod.com/ux/ats/careersite/1/job/X?c=acme"
    )


# --- Pagination -------------------------------------------------------------


def test_pagination_via_total_count(httpx_mock) -> None:
    """totalCount=60 with PAGE_SIZE=25 → 3 pages total. Page 1 from first
    init call, pages 2-3 from fan-out."""
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL,
        json=_search_response([_req(req_id=str(i)) for i in range(25)], total=60),
        is_reusable=True,
    )
    jobs = CornerstoneScraper("acme").fetch()
    # Reused mock returns same 25 reqs every page → dedup keeps 25 unique
    assert len({j.ats_id for j in jobs}) == 25


def test_no_pagination_when_total_le_page_size(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


# --- Error handling ---------------------------------------------------------


def test_search_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.cornerstone as cs
    monkeypatch.setattr(cs, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, status_code=503)
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_honored(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.cornerstone as cs
    monkeypatch.setattr(cs, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL, status_code=429, headers={"Retry-After": "11"}
    )
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    CornerstoneScraper("acme").fetch()
    assert 11.0 in sleeps


def test_malformed_json_raises_clean_error(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, text="<html>not json</html>")
    with pytest.raises(ScraperError, match="malformed JSON"):
        CornerstoneScraper("acme").fetch()
