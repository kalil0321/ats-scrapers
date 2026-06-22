"""Tests for the Y Combinator scraper.

Pin the two-step discovery flow (companies API → per-company HTML
+ embedded ``jobPostings`` JSON) and the YC-specific parsers
(salary range '$180K - $250K', '16 days' relative createdAt,
HTML-entity-encoded JSON in the page).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, YCombinatorScraper
from jobhive.scrapers.ycombinator import (
    _employment_from_type,
    _extract_balanced_array,
    _parse_min_experience,
    _parse_relative_age,
    _parse_salary_range,
)

_COMPANIES_RE = re.compile(r"^https://api\.ycombinator\.com/v0\.1/companies")
_PAGE_RE = re.compile(r"^https://www\.ycombinator\.com/companies/[a-z0-9-]+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.ycombinator as yc
    monkeypatch.setattr(yc, "MAX_RETRIES", 1)
    monkeypatch.setattr(yc, "RETRY_BASE_DELAY", 0.0)


# --- helpers ----------------------------------------------------------------


def _api_page(slugs: list[str], *, page: int = 1, total_pages: int = 1) -> dict:
    return {
        "companies": [{"slug": s, "name": s.title()} for s in slugs],
        "page": page,
        "totalPages": total_pages,
        "nextPage": (
            f"https://api.ycombinator.com/v0.1/companies?isHiring=true&page={page+1}"
            if page < total_pages else None
        ),
    }


def _company_page_html(jobs: list[dict[str, Any]]) -> str:
    """Build a YC company page with the ``jobPostings`` JSON embedded
    HTML-entity-encoded the same way the real page does it."""
    import json
    payload = json.dumps(jobs)
    # Escape just the chars YC's renderer encodes — quotes, &, <, >.
    encoded = (
        payload.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
    return (
        '<html><body><script>'
        f'window.__DATA__ = {{&quot;jobPostings&quot;:{encoded[encoded.index("[") :]}}}'
        '</script></body></html>'
    )


def _job(
    *,
    job_id: int = 93354,
    title: str = "Founding Engineer",
    company_name: str = "Acme",
    company_slug: str = "acme",
    location: str = "San Francisco, CA, US",
    salary: str = "$180K - $250K",
    job_type: str = "Full-time",
    role: str = "engineering",
    pretty_role: str = "Engineering",
    min_experience: str = "3+ years",
    created_at: str = "16 days",
    apply_url: str = "https://account.ycombinator.com/authenticate?continue=https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D93354",
    description: str = "Build the core product.",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "url": f"/companies/{company_slug}/jobs/AbC123-{title.lower().replace(' ', '-')}",
        "applyUrl": apply_url,
        "companyName": company_name,
        "companyBatchName": "S20",
        "companyOneLiner": "Build things.",
        "companyUrl": f"/companies/{company_slug}",
        "location": location,
        "type": job_type,
        "role": role,
        "prettyRole": pretty_role,
        "salaryRange": salary,
        "minExperience": min_experience,
        "createdAt": created_at,
        "description": description,
        "skills": [],
    }


# --- helpers under test ----------------------------------------------------


def test_balanced_array_extraction() -> None:
    text = '...stuff... "jobPostings":[{"a":1, "b":[2,3]}, {"c":"]"}] ...rest...'
    out = _extract_balanced_array(text, '"jobPostings":')
    assert out == '[{"a":1, "b":[2,3]}, {"c":"]"}]'


def test_balanced_array_returns_none_when_marker_missing() -> None:
    assert _extract_balanced_array("no marker here", '"jobPostings":') is None


@pytest.mark.parametrize("raw, expected", [
    ("$180K - $250K", (180000, 250000, "USD")),
    ("$120K", (120000, 120000, "USD")),
    ("$1.5M - $2M", (1_500_000, 2_000_000, "USD")),
    ("Equity only", (None, None, None)),
    ("", (None, None, None)),
    (None, (None, None, None)),
])
def test_parse_salary_range(raw, expected) -> None:
    assert _parse_salary_range(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("3+ years", 3),
    ("0 years", 0),
    ("10+ years", 10),
    ("", None),
    (None, None),
])
def test_parse_min_experience(raw, expected) -> None:
    assert _parse_min_experience(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Full-time", "FULL_TIME"),
    ("Part-time", "PART_TIME"),
    ("Internship", "INTERN"),
    ("Contract", "CONTRACT"),
    ("Volunteer", None),
])
def test_employment_from_type(raw, expected) -> None:
    assert _employment_from_type(raw) == expected


def test_parse_relative_age_returns_recent_for_days() -> None:
    """`'16 days'` → datetime ~16 days before now."""
    from datetime import datetime
    out = _parse_relative_age("16 days")
    assert out is not None
    delta = (datetime.now() - out).days
    assert 15 <= delta <= 17


def test_parse_relative_age_handles_invalid() -> None:
    assert _parse_relative_age("yesterday") is None
    assert _parse_relative_age("") is None


# --- registry / wiring -----------------------------------------------------


def test_registry_resolves_yc() -> None:
    assert ScraperRegistry.get(ATSType.YCOMBINATOR) is YCombinatorScraper


# --- happy path -------------------------------------------------------------


def test_walks_companies_then_extracts_postings(httpx_mock) -> None:
    """One company → one job. Verify every populated Job field maps
    to the right YC field."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([_job()]),
    )
    jobs = YCombinatorScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.YCOMBINATOR
    assert j.ats_id == "93354"
    assert j.title == "Founding Engineer"
    assert j.company == "Acme"
    assert j.location == "San Francisco, CA, US"
    assert j.salary_min == 180000
    assert j.salary_max == 250000
    assert j.salary_currency == "USD"
    assert j.experience == 3
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full-time"
    assert j.department == "Engineering"
    assert j.description == "Build the core product.\n\nBuild things."
    assert j.posted_at is not None  # parsed from "16 days"
    assert str(j.url).endswith("/companies/acme/jobs/AbC123-founding-engineer")
    assert "signup_job_id" in str(j.apply_url)


# --- multi-company / multi-job ---------------------------------------------


def test_scrapes_multiple_companies(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme", "beta"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([_job(job_id=1, title="Eng 1", company_slug="acme")]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/beta",
        text=_company_page_html([_job(job_id=2, title="Eng 2", company_slug="beta")]),
    )
    jobs = YCombinatorScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2"}


def test_dedupes_jobs_with_same_id(httpx_mock) -> None:
    """Same job ID showing up on multiple company pages → single Job."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme", "beta"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([_job(job_id=42, title="Shared")]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/beta",
        text=_company_page_html([_job(job_id=42, title="Shared")]),
    )
    jobs = YCombinatorScraper("any").fetch()
    assert len(jobs) == 1


# --- discovery edge cases ---------------------------------------------------


def test_paginates_companies_until_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme"], page=1, total_pages=2),
    )
    httpx_mock.add_response(
        url=re.compile(r".*page=2.*"),
        json=_api_page(["beta"], page=2, total_pages=2),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([_job(job_id=1)]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/beta",
        text=_company_page_html([_job(job_id=2)]),
    )
    jobs = YCombinatorScraper("any").fetch()
    assert len(jobs) == 2


def test_company_404_treated_as_no_jobs(httpx_mock) -> None:
    """A company removed since discovery 404s on /companies/{slug} —
    must not crash the whole run; just skip."""
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme", "removed"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([_job(job_id=1)]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/removed",
        status_code=404,
    )
    jobs = YCombinatorScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_company_page_with_no_jobpostings_block(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text="<html><body>no jobs here</body></html>",
    )
    jobs = YCombinatorScraper("any").fetch()
    assert jobs == []


# --- defensive --------------------------------------------------------------


def test_drops_postings_missing_required_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*page=1.*"),
        json=_api_page(["acme"]),
    )
    httpx_mock.add_response(
        url="https://www.ycombinator.com/companies/acme",
        text=_company_page_html([
            _job(job_id=1, title="Good"),
            {"id": 2, "url": "/x"},  # no title
            {"title": "no id", "url": "/x"},  # no id
            {"id": 3, "title": "no url"},  # no url
        ]),
    )
    jobs = YCombinatorScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_persistent_500_on_companies_api_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_COMPANIES_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        YCombinatorScraper("any").fetch()
