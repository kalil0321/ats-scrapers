"""Tests for the Hiredly Malaysia scraper.

Hiredly serves the same Next.js page-props payload on the ``/jobs`` HTML
page and on ``_next/data/{buildId}/jobs.json``. Tests cover:

- The buildId is discovered from the initial HTML fetch
- The data endpoint is hit per-page with the discovered buildId
- A 404 mid-walk triggers re-discovery (buildId rotation) and resumes
- Job-payload mapping → Job model fields (title, slug→url, salary…)
- Pagination terminates on consecutive empty pages
"""

from __future__ import annotations

import json
import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import HiredlyScraper, ScraperRegistry
from jobhive.scrapers.hiredly import (
    _employment_type,
    _parse_salary_range,
)

_LISTING_URL = "https://my.hiredly.com/jobs"
_DATA_RE = re.compile(
    r"^https://my\.hiredly\.com/_next/data/[^/]+/jobs\.json\?page=\d+$"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.hiredly as h
    monkeypatch.setattr(h, "MAX_RETRIES", 1)
    monkeypatch.setattr(h, "RETRY_BASE_DELAY", 0.0)


def _listing_html(build_id: str = "buildA") -> str:
    """Minimal HTML page that embeds the buildId in the same shape
    Next.js emits on the real my.hiredly.com page."""
    return (
        "<html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"buildId": build_id})
        + "</script></body></html>"
    )


def _job(
    *,
    id: str = "4d8c9f48-563c-4360-82b7-51dfb8156b87",
    title: str = "Sales Executive (Interior / Renovation)",
    slug: str = "jobs-malaysia-yz-job-sales-executive",
    company_name: str = "YZ HORIZON SDN BHD (SPACE MAKERS)",
    company_slug: str = "yz-horizon-sdn-bhd-space-makers",
    company_id: str = "b98c8f65-5b12-439e-b345-2be4d9553eb0",
    state_region: str = "Selangor",
    location: str = "13 Jalan P2/3, Semenyih, Selangor",
    salary: str = "3000 - 4000",
    job_type: str = "Full-Time",
    active_at: str = "2026-04-30T18:46:00+08:00",
    skills: list[str] | None = None,
    tracks: list[str] | None = None,
    career_level: str = "Junior Executive",
    gpt_summary: str | None = "Great role at SPACE MAKERS.",
    min_years_experience: int = 0,
    category: str = "organic",
) -> dict:
    skills = skills if skills is not None else [
        "Sales Management", "Negotiation",
    ]
    tracks = tracks if tracks is not None else ["Business Development"]
    return {
        "id": id,
        "title": title,
        "slug": slug,
        "company": {
            "id": company_id,
            "name": company_name,
            "slug": company_slug,
        },
        "stateRegion": state_region,
        "location": location,
        "salary": salary,
        "jobType": job_type,
        "activeAt": active_at,
        "skills": [{"name": s} for s in skills],
        "tracks": [{"id": str(i), "title": t} for i, t in enumerate(tracks)],
        "careerLevel": career_level,
        "gptSummary": gpt_summary,
        "minYearsExperience": str(min_years_experience),
        "category": category,
    }


def _data_response(jobs: list[dict]) -> dict:
    return {"pageProps": {"jobs": jobs}, "__N_SSP": True}


# --- registry ---------------------------------------------------------------


def test_registry_resolves_hiredly() -> None:
    assert ScraperRegistry.get(ATSType.HIREDLY) is HiredlyScraper


# --- buildId discovery + happy path ----------------------------------------


def test_discovers_build_id_and_walks_pages(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html("buildA"))
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=1",
        json=_data_response([_job(id="job-1", slug="jobs-malaysia-x-job-a")]),
    )
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=2",
        json=_data_response([]),
    )
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=3",
        json=_data_response([]),
    )

    jobs = HiredlyScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.HIREDLY
    assert j.ats_id == "job-1"
    assert j.title == "Sales Executive (Interior / Renovation)"
    assert str(j.url) == (
        "https://my.hiredly.com/jobs/jobs-malaysia-x-job-a"
    )
    assert j.country_iso == "MY"
    assert j.language == "en"


def test_maps_payload_fields_to_canonical_slots(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html())
    httpx_mock.add_response(
        url=_DATA_RE,
        json=_data_response([_job()]),
    )
    # Subsequent empty pages terminate pagination.
    httpx_mock.add_response(url=_DATA_RE, json=_data_response([]),
                            is_reusable=True)

    jobs = HiredlyScraper("any").fetch()
    j = jobs[0]
    assert j.company == "YZ HORIZON SDN BHD (SPACE MAKERS)"
    assert j.location == "Selangor"  # stateRegion preferred over street
    assert j.salary_summary == "3000 - 4000"
    assert j.salary_currency == "MYR"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 3000.0
    assert j.salary_max == 4000.0
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full-Time"
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw["skills"] == ["Sales Management", "Negotiation"]
    assert j.raw["tracks"] == ["Business Development"]
    assert j.raw["company_slug"] == "yz-horizon-sdn-bhd-space-makers"
    assert j.raw["career_level"] == "Junior Executive"
    assert j.raw["state_region"] == "Selangor"


def test_falls_back_to_location_when_state_region_missing(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html())
    httpx_mock.add_response(
        url=_DATA_RE,
        json=_data_response([_job(state_region="", location="Kuala Lumpur")]),
    )
    httpx_mock.add_response(url=_DATA_RE, json=_data_response([]),
                            is_reusable=True)
    jobs = HiredlyScraper("any").fetch()
    assert jobs[0].location == "Kuala Lumpur"


# --- pagination -------------------------------------------------------------


def test_paginates_across_multiple_pages(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html())
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=1",
        json=_data_response([_job(id="a"), _job(id="b")]),
    )
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=2",
        json=_data_response([_job(id="c")]),
    )
    # Two consecutive empty pages stop the walk.
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=3",
        json=_data_response([]),
    )
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=4",
        json=_data_response([]),
    )
    jobs = HiredlyScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"a", "b", "c"}


def test_max_pages_caps_walk(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html())
    # Stub a fresh job per page; only ``max_pages`` will stop us.
    for p in (1, 2, 3):
        httpx_mock.add_response(
            url=f"https://my.hiredly.com/_next/data/buildA/jobs.json?page={p}",
            json=_data_response([_job(id=f"id-{p}")]),
        )
    jobs = HiredlyScraper("any", max_pages=3).fetch()
    assert {j.ats_id for j in jobs} == {"id-1", "id-2", "id-3"}


# --- buildId rotation -------------------------------------------------------


def test_re_discovers_build_id_on_404(httpx_mock) -> None:
    """If the buildId rotates mid-walk, ``_next/data/{old}/...`` starts
    returning 404. We refetch ``/jobs`` to get the new buildId and retry."""
    # Initial discovery → buildA
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html("buildA"))
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=1",
        json=_data_response([_job(id="a")]),
    )
    # Page 2 with the old build → 404 (rotated mid-walk)
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildA/jobs.json?page=2",
        status_code=404,
    )
    # Re-discovery → buildB
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html("buildB"))
    httpx_mock.add_response(
        url="https://my.hiredly.com/_next/data/buildB/jobs.json?page=2",
        json=_data_response([_job(id="b")]),
    )
    # Two empties end the walk.
    httpx_mock.add_response(
        url=re.compile(
            r"^https://my\.hiredly\.com/_next/data/buildB/jobs\.json\?page=[3-9]$"
        ),
        json=_data_response([]),
        is_reusable=True,
    )
    jobs = HiredlyScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"a", "b"}


def test_missing_build_id_raises(httpx_mock) -> None:
    """The buildId regex must match; an HTML page without it is fatal."""
    httpx_mock.add_response(
        url=_LISTING_URL, text="<html>no nextjs here</html>",
    )
    with pytest.raises(ScraperError, match="buildId"):
        HiredlyScraper("any").fetch()


def test_500_on_data_endpoint_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_URL, text=_listing_html())
    httpx_mock.add_response(url=_DATA_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        HiredlyScraper("any").fetch()


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("3000 - 4000", (3000.0, 4000.0)),
    ("3000-4000", (3000.0, 4000.0)),
    ("3,000 – 4,500", (3000.0, 4500.0)),
    ("4500", (4500.0, 4500.0)),
    ("", (None, None)),
    (None, (None, None)),
    ("Negotiable", (None, None)),
])
def test_parse_salary_range(raw, expected) -> None:
    assert _parse_salary_range(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Full-Time", "FULL_TIME"),
    ("Part Time", "PART_TIME"),
    ("Contract", "CONTRACT"),
    ("Internship", "INTERN"),
    ("Temporary", "TEMPORARY"),
    ("Volunteer", None),
    ("", None),
    (None, None),
])
def test_employment_type_normalization(raw, expected) -> None:
    assert _employment_type(raw) == expected
