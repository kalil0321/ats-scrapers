"""Tests for the Wanted (KR + JP) scraper.

Pin the parsing contract (each Wanted v4 field → Job field) and the
cursor-based pagination behaviour. The API uses ``links.next`` rather
than offset arithmetic, so the test suite has to follow that style.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, WantedScraper

_API_RE = re.compile(r"^https://www\.wanted\.co\.kr/api/v4/jobs(?:\?.*)?$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch, httpx_mock) -> None:
    import jobhive.scrapers.wanted as w
    monkeypatch.setattr(w, "MAX_RETRIES", 1)
    monkeypatch.setattr(w, "RETRY_BASE_DELAY", 0.0)
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.wanted\.co\.kr/api/v4/jobs/\d+$"),
        json={
            "job": {
                "detail": {
                    "intro": "Build hiring products.",
                    "main_tasks": "Own the platform.",
                    "requirements": "Python experience.",
                }
            }
        },
        is_reusable=True,
        is_optional=True,
    )


def _job(
    *,
    job_id: int,
    position: str,
    company_name: str = "Acme",
    company_id: int = 42,
    industry: str | None = "Tech",
    location: str = "서울",
    country_kor: str = "한국",
    district: str = "중구",
    full_location: str = "서울 중구 신당동 340-44",
    annual_from: int | None = 4,
    annual_to: int | None = 6,
    category_tags: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "position": position,
        "due_time": None,
        "status": "active",
        "address": {
            "country": country_kor,
            "location": location,
            "district": district,
            "full_location": full_location,
            "location_key": "seoul",
            "district_key": "seoul.jung-gu",
        },
        "company": {
            "id": company_id,
            "name": company_name,
            "industry_name": industry,
        },
        "annual_from": annual_from,
        "annual_to": annual_to,
        "category_tags": category_tags or [{"parent_id": 517, "id": 643}],
        "logo_img": {"origin": "x", "thumb": "y"},
    }


def _page(
    items: list[dict[str, Any]],
    *,
    next_offset: int | None = None,
    country: str = "kr",
) -> dict:
    """Build a mock API response with optional next-page cursor."""
    links = {"prev": None, "next": None}
    if next_offset is not None:
        links["next"] = (
            f"/api/v4/jobs?country={country}&locations=all&years=-1"
            f"&limit=100&offset={next_offset}"
        )
    return {"data": items, "links": links, "model_status": None}


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_wanted() -> None:
    assert ScraperRegistry.get(ATSType.WANTED) is WantedScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_v4_job_payload(httpx_mock) -> None:
    """Single-page KR scrape; verify every populated Job field maps to the
    right v4 field."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_job(job_id=359771, position="HR Manager")]),
    )

    jobs = WantedScraper("any", country_codes=["kr"]).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.WANTED
    assert j.ats_id == "359771"
    assert j.title == "HR Manager"
    assert j.company == "Acme"
    # Location is district-first, then city, then country (Wanted's KR
    # listings ship Korean strings — keep them verbatim).
    assert j.location == "중구, 서울, 한국"
    assert j.experience == 4  # annual_from
    assert j.description == "Build hiring products.\n\nOwn the platform.\n\nPython experience."
    assert j.raw is not None
    assert j.raw.get("annual_to") == 6
    assert j.raw.get("industry_name") == "Tech"
    assert j.raw.get("country") == "KR"
    assert str(j.url) == "https://www.wanted.co.kr/wd/359771"


# --- pagination -------------------------------------------------------------


def test_paginates_via_links_next_cursor(httpx_mock) -> None:
    """v4 uses cursor-style pagination — follow ``links.next`` until null."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job(job_id=i, position=f"Job {i}") for i in range(100)],
            next_offset=100,
        ),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job(job_id=i, position=f"Job {i}") for i in range(100, 150)],
            next_offset=None,  # last page
        ),
    )

    jobs = WantedScraper("any", country_codes=["kr"]).fetch()
    assert len(jobs) == 150
    assert {j.ats_id for j in jobs} == {str(i) for i in range(150)}


def test_dedupes_overlapping_pages(httpx_mock) -> None:
    """Sometimes Wanted's cursor walk re-includes a tail item — dedup
    must collapse them on ``ats_id``."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            [_job(job_id=i, position=f"Job {i}") for i in range(20)],
            next_offset=20,
        ),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page(
            # Page 2 repeats ids 18 and 19; new ids 20..24
            [_job(job_id=i, position=f"Job {i}") for i in [18, 19, 20, 21, 22, 23, 24]],
            next_offset=None,
        ),
    )
    jobs = WantedScraper("any", country_codes=["kr"]).fetch()
    assert len({j.ats_id for j in jobs}) == 25


# --- multi-country ----------------------------------------------------------


def test_default_scrapes_kr_and_jp(httpx_mock) -> None:
    """Default ``country_codes`` is (kr, jp). Both should be hit."""
    httpx_mock.add_response(
        url=re.compile(r".*country=kr.*"),
        json=_page([_job(job_id=1, position="KR job")]),
    )
    httpx_mock.add_response(
        url=re.compile(r".*country=jp.*"),
        json=_page([_job(job_id=2, position="JP job", country_kor="日本", location="東京")]),
    )

    jobs = WantedScraper("any").fetch()
    countries_seen = {j.raw["country"] for j in jobs if j.raw and "country" in j.raw}
    assert countries_seen == {"KR", "JP"}


def test_unsupported_country_returns_empty_not_crash(httpx_mock) -> None:
    """v4 returns 422 for any country outside {kr, jp}. Treat as
    'this slice has no data', don't crash the whole run."""
    httpx_mock.add_response(
        url=re.compile(r".*country=us.*"),
        status_code=422,
    )
    jobs = WantedScraper("any", country_codes=["us"]).fetch()
    assert jobs == []


# --- field handling ---------------------------------------------------------


def test_skips_jobs_missing_id_or_position(httpx_mock) -> None:
    """Defensive: if Wanted ever returns a malformed entry (missing id or
    position), drop it rather than emitting half-built rows."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _job(job_id=1, position="Good"),
            {"id": 2, "address": {}, "company": {"name": "Acme"}},  # no position
            {"position": "No id", "address": {}, "company": {"name": "Acme"}},
        ]),
    )
    jobs = WantedScraper("any", country_codes=["kr"]).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_falls_back_to_full_location_when_structured_parts_missing(httpx_mock) -> None:
    """Some postings have ``full_location`` but no district/location/country."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([{
            "id": 1,
            "position": "Engineer",
            "address": {
                "country": "",  # blank
                "location": "",
                "district": "",
                "full_location": "Singapore",
            },
            "company": {"id": 1, "name": "Acme"},
            "annual_from": None,
            "annual_to": None,
        }]),
    )
    jobs = WantedScraper("any", country_codes=["kr"]).fetch()
    assert jobs[0].location == "Singapore"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """Real server failures should surface, not silently emit []."""
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        WantedScraper("any", country_codes=["kr"]).fetch()
