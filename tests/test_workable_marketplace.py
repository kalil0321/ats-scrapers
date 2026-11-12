"""Tests for the Workable marketplace scraper.

Pin the cursor walk (pageToken), the stop conditions, the partial-walk
guard, and the parsing of one marketplace posting.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, WorkableMarketplaceScraper

API_RE = re.compile(r"https://jobs\.workable\.com/api/v1/jobs\?.*")


def _job(job_id: str = "91844176-bf2e-4a68-bc7a-d8f67ea2ee01", **over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": job_id,
        "title": "Customer Support Specialist",
        "state": "published",
        "department": "Customer Service",
        "description": "<p>We offer <strong>great</strong> pay.</p><p>Second paragraph.</p>",
        "requirementsSection": "<ul><li>Clear writing</li><li>Patience</li></ul>",
        "benefitsSection": "<p>Health insurance</p>",
        "employmentType": "Full-time",
        "url": f"https://jobs.workable.com/view/{job_id}/support",
        "language": "en",
        "locations": ["TELECOMMUTE", "Boise, Idaho, United States"],
        "location": {"city": "Boise", "subregion": "Idaho", "countryName": "United States"},
        "created": "2026-11-11T14:18:38.696Z",
        "updated": "2026-11-11T14:18:38.696Z",
        "company": {
            "id": "3c8b00b3-5552-4c8d-b31c-84f87847e781",
            "title": "EconRam Systems",
            "website": "https://www.econram.net/",
            "url": "https://jobs.workable.com/company/8tBB/jobs-at-econram-systems",
        },
        "workplace": "remote",
    }
    item.update(over)
    return item


def _page(jobs: list[dict[str, Any]], token: str | None, total: int) -> dict[str, Any]:
    page: dict[str, Any] = {"title": "Jobs", "totalSize": total, "jobs": jobs}
    if token is not None:
        page["nextPageToken"] = token
    return page


def _scraper(**kw: Any) -> WorkableMarketplaceScraper:
    return WorkableMarketplaceScraper("any", page_delay=0, **kw)


def test_registry_resolves_marketplace() -> None:
    assert ScraperRegistry.get(ATSType.WORKABLE_MARKETPLACE) is WorkableMarketplaceScraper


def test_walks_cursor_until_last_page(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job("a"), _job("b")], "t1", 3))
    httpx_mock.add_response(url=API_RE, json=_page([_job("c")], None, 3))
    jobs = _scraper().fetch()
    assert [j.ats_id for j in jobs] == ["a", "b", "c"]
    second = httpx_mock.get_requests()[1]
    assert second.url.params["pageToken"] == "t1"


def test_stops_on_empty_page(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job("a")], "t1", 1))
    httpx_mock.add_response(url=API_RE, json=_page([], "t2", 1))
    assert [j.ats_id for j in _scraper().fetch()] == ["a"]


def test_dedupes_repeated_postings(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job("a"), _job("b")], "t1", 2))
    httpx_mock.add_response(url=API_RE, json=_page([_job("b")], None, 2))
    assert [j.ats_id for j in _scraper().fetch()] == ["a", "b"]


def test_repeated_token_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job("a")], "same", 2))
    httpx_mock.add_response(url=API_RE, json=_page([_job("b")], "same", 2))
    with pytest.raises(ScraperError, match="same page token"):
        _scraper().fetch()


def test_partial_walk_raises(httpx_mock) -> None:
    """A walk that ends with far fewer postings than ``totalSize`` must
    fail instead of publishing a fraction of the marketplace."""
    httpx_mock.add_response(url=API_RE, json=_page([_job("a")], None, 1000))
    with pytest.raises(ScraperError, match="less than half"):
        _scraper().fetch()


def test_max_pages_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job("a")], "t1", 2))
    with pytest.raises(ScraperError, match="max_pages"):
        _scraper(max_pages=1).fetch()


def test_bad_shape_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json={"unexpected": True})
    with pytest.raises(ScraperError, match="shape changed"):
        _scraper().fetch()


def test_parses_posting(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job()], None, 1))
    j = _scraper().fetch()[0]
    assert j.ats_type is ATSType.WORKABLE_MARKETPLACE
    assert j.ats_id == "91844176-bf2e-4a68-bc7a-d8f67ea2ee01"
    assert j.title == "Customer Support Specialist"
    assert j.company == "EconRam Systems"
    assert j.location == "Boise, Idaho, United States"
    assert j.country_iso == "US"
    assert j.is_remote is True
    assert j.department == "Customer Service"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full-time"
    assert j.language == "en"
    assert j.posted_at == datetime(2026, 11, 11, 14, 18, 38, 696000, tzinfo=UTC)
    assert j.raw is not None
    assert j.raw["company_website"] == "https://www.econram.net/"


def test_description_keeps_inline_text_on_one_line(httpx_mock) -> None:
    httpx_mock.add_response(url=API_RE, json=_page([_job()], None, 1))
    desc = _scraper().fetch()[0].description
    assert desc is not None
    assert "We offer great pay." in desc
    assert "Second paragraph." in desc
    assert "Requirements\n- Clear writing\n- Patience" in desc
    assert "Benefits\nHealth insurance" in desc
    assert "<" not in desc


def test_unknown_country_left_unset(httpx_mock) -> None:
    job = _job(location={"city": "Nowhere", "countryName": "Atlantis"})
    httpx_mock.add_response(url=API_RE, json=_page([job], None, 1))
    j = _scraper().fetch()[0]
    assert j.country_iso is None
    assert j.location == "Nowhere, Atlantis"


def test_on_site_and_unknown_workplace(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_RE,
        json=_page(
            [_job("a", workplace="on_site"), _job("b", workplace=None)], None, 2,
        ),
    )
    by_id = {j.ats_id: j for j in _scraper().fetch()}
    assert by_id["a"].is_remote is False
    assert by_id["b"].is_remote is None


def test_skips_unpublished_and_incomplete(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API_RE,
        json=_page(
            [_job("a"), _job("b", state="closed"), _job("c", title=""), {"id": "d"}],
            None,
            1,
        ),
    )
    assert [j.ats_id for j in _scraper().fetch()] == ["a"]
