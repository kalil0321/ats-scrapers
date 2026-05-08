"""Tests for The Muse scraper."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, TheMuseScraper

_API_RE = re.compile(r"^https://www\.themuse\.com/api/public/jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.themuse as m
    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


def _job(
    *,
    job_id: int = 1,
    name: str = "Software Engineer",
    company: str = "Acme",
    company_id: int = 100,
    locations: list[str] | None = None,
    level: str | None = "Senior Level",
    landing_page: str = "https://www.themuse.com/jobs/acme/sw-eng-1",
    pub_date: str = "2026-03-30T16:51:26Z",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": job_id,
        "name": name,
        "company": {"id": company_id, "name": company, "short_name": company.lower()},
        "locations": [{"name": loc} for loc in (locations or ["New York, NY"])],
        "categories": [{"name": "Software Engineering"}],
        "publication_date": pub_date,
        "refs": {"landing_page": landing_page},
        "contents": "<p>Build things.</p>",
        "tags": [],
        "type": "external",
    }
    if level:
        out["levels"] = [{"name": level, "short_name": level.lower().replace(" ", "_")}]
    return out


def _envelope(jobs: list[dict], page: int = 0, total: int = 1, page_count: int = 1) -> dict:
    return {
        "results": jobs,
        "page": page,
        "page_count": page_count,
        "total": total,
        "items_per_page": 20,
    }


def test_registry_resolves_themuse() -> None:
    assert ScraperRegistry.get(ATSType.THEMUSE) is TheMuseScraper


def test_parses_full_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([_job()]),
    )
    j = TheMuseScraper("any", max_pages=1).fetch()[0]
    assert j.ats_type is ATSType.THEMUSE
    assert j.ats_id == "1"
    assert j.title == "Software Engineer"
    assert j.company == "Acme"
    assert j.location == "New York, NY"
    assert j.commitment == "Senior Level"
    assert j.description == "Build things."
    assert j.posted_at is not None
    assert str(j.url) == "https://www.themuse.com/jobs/acme/sw-eng-1"


@pytest.mark.parametrize("level", ["Internship", "Entry Level", "Mid Level", "Senior Level", "Director"])
def test_level_passes_through_to_commitment(level: str, httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([_job(job_id=1, level=level)]),
    )
    assert TheMuseScraper("any", max_pages=1).fetch()[0].commitment == level


def test_multi_location_first_in_location_rest_in_raw(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([_job(job_id=1, locations=["NYC", "Boston", "Austin"])]),
    )
    j = TheMuseScraper("any", max_pages=1).fetch()[0]
    assert j.location == "NYC"
    assert j.raw is not None
    assert j.raw["additional_locations"] == ["Boston", "Austin"]


def test_pagination_fans_out_pages(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([_job(job_id=1)]),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_envelope([_job(job_id=2)]),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2.*"),
        json=_envelope([_job(job_id=3)]),
    )
    jobs = TheMuseScraper("any", max_pages=3).fetch()
    assert len(jobs) == 3


def test_max_pages_clamped_to_ceiling() -> None:
    """Even if the user asks for max_pages=500, the API caps at page=99
    so we clamp to 100 (0..99 inclusive)."""
    s = TheMuseScraper("any", max_pages=500)
    assert s.max_pages == 100  # DEFAULT_MAX_PAGES = 100


def test_400_past_cap_treated_as_no_more_data(httpx_mock) -> None:
    """Page 100 returns 400 — must not crash the run; just yields no
    more data."""
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([_job(job_id=1)]),
    )
    # Pages 1+ all 400 (simulating past-cap)
    httpx_mock.add_response(url=_API_RE, status_code=400, is_reusable=True)
    jobs = TheMuseScraper("any", max_pages=5).fetch()
    assert len(jobs) == 1


def test_drops_jobs_missing_required_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=0.*"),
        json=_envelope([
            _job(job_id=1),
            {"id": 2, "name": "no url", "company": {"name": "X"}, "locations": [],
             "refs": {"landing_page": ""}},
            {"id": 3, "company": {"name": "X"}, "locations": [],
             "refs": {"landing_page": "https://x"}},  # no name
        ]),
    )
    jobs = TheMuseScraper("any", max_pages=1).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        TheMuseScraper("any", max_pages=1).fetch()
