"""Tests for the Shine.com (India) scraper.

Pin parsing of the embedded ``__NEXT_DATA__`` payload (one row per
``jsrp.searchresult.data.results`` entry), the ``all-jobs-N`` pagination
shape, and the experience-range / location / India ISO inference.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, ShineScraper

_LISTING_RE = re.compile(r"^https://www\.shine\.com/job-search/all-jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.shine as s
    monkeypatch.setattr(s, "MAX_RETRIES", 1)
    monkeypatch.setattr(s, "RETRY_BASE_DELAY", 0.0)


# --- payload builders -------------------------------------------------------


def _result(
    *,
    job_id: int = 18798429,
    title: str = "Wealth Management",
    company: str = "JPMorgan Chase & Co.",
    slug: str | None = None,
    locations: list[str] | None = None,
    description: str = "Role Overview: A great gig.",
    experience: str = "2 to 6 Yrs",
    posted_at: str = "2026-03-23T00:08:47",
    skills: str = "Portfolio Analysis, Client Service, Risk Analysis",
    industry: str = "BFSI",
    salary: str = "[Salary Hidden]",
    job_type: int = 1,
) -> dict[str, Any]:
    if slug is None:
        slug_title = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug_company = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
        slug = f"{slug_title}/{slug_company}/{job_id}"
    return {
        "id": job_id,
        "jJT": title,
        "jCName": company,
        "jSlug": slug,
        "jLoc": locations if locations is not None else ["All India"],
        "jJD": description,
        "jExp": experience,
        "jPDate": posted_at,
        "jKwd": skills,
        "jInd": industry,
        "jSal": salary,
        "jJobType": job_type,
        "jEType": 1,
    }


def _page(
    results: list[dict[str, Any]],
    *,
    page: int = 1,
    count: int = 100,
    num_pages: int = 1,
) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialState": {
                    "jsrp": {
                        "searchresult": {
                            "isLoaded": True,
                            "data": {
                                "count": count,
                                "next": page < num_pages,
                                "previous": None if page == 1 else True,
                                "results": results,
                                "num_pages": num_pages,
                                "page": page,
                            },
                        },
                    },
                },
            },
        },
    }
    body = json.dumps(payload)
    return (
        "<!DOCTYPE html><html><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{body}</script>'
        "</body></html>"
    )


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_shine() -> None:
    assert ScraperRegistry.get(ATSType.SHINE) is ShineScraper


def test_ats_type_value() -> None:
    assert ATSType.SHINE.value == "shine"


# --- happy path -------------------------------------------------------------


def test_parses_full_shine_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result()]),
    )

    jobs = ShineScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.SHINE
    assert j.ats_id == "18798429"
    assert j.title == "Wealth Management"
    assert j.company == "JPMorgan Chase & Co."
    assert j.location == "All India"
    assert j.country_iso == "IN"
    assert j.language == "en"
    assert j.experience == 2  # min from "2 to 6 Yrs"
    assert j.employment_type == "FULL_TIME"
    assert j.salary_summary is None  # "[Salary Hidden]" is dropped
    assert j.description == "Role Overview: A great gig."
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 3
    assert str(j.url) == (
        "https://www.shine.com/jobs/wealth-management/"
        "jpmorgan-chase-co/18798429"
    )
    assert j.raw is not None
    assert j.raw.get("experience_min") == 2
    assert j.raw.get("experience_max") == 6
    assert j.raw.get("industry") == "BFSI"
    assert "Portfolio Analysis" in (j.raw.get("skills") or [])
    assert j.global_id == "shine:18798429"


# --- pagination -------------------------------------------------------------


def test_paginates_via_all_jobs_dash_n(httpx_mock) -> None:
    """Page 1 lives at ``/job-search/all-jobs`` (no suffix), page 2 at
    ``-2``, etc. ``num_pages`` from page 1 tells us how far to go."""
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs",
        html=_page(
            [_result(job_id=1, title="One"), _result(job_id=2, title="Two")],
            page=1, num_pages=3,
        ),
    )
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs-2",
        html=_page(
            [_result(job_id=3, title="Three")],
            page=2, num_pages=3,
        ),
    )
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs-3",
        html=_page(
            [_result(job_id=4, title="Four")],
            page=3, num_pages=3,
        ),
    )

    jobs = ShineScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3", "4"}


def test_respects_max_pages_cap(httpx_mock) -> None:
    """``num_pages`` may report 12k+ on the live site — instance cap
    must hard-stop the sweep regardless."""
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs",
        html=_page(
            [_result(job_id=1)], page=1, num_pages=100,
        ),
    )
    # No mock for page 2 onwards — if the scraper requested them, the
    # mock library would raise.
    jobs = ShineScraper("any", max_pages=1).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_dedupes_overlapping_pages(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs",
        html=_page(
            [_result(job_id=i) for i in range(5)],
            page=1, num_pages=2,
        ),
    )
    httpx_mock.add_response(
        url="https://www.shine.com/job-search/all-jobs-2",
        html=_page(
            # Repeats ids 3 and 4 from page 1.
            [_result(job_id=i) for i in [3, 4, 5, 6]],
            page=2, num_pages=2,
        ),
    )
    jobs = ShineScraper("any").fetch()
    assert sorted(j.ats_id for j in jobs) == ["0", "1", "2", "3", "4", "5", "6"]


# --- location / country inference -------------------------------------------


def test_all_india_marker_sets_country_iso_in(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(locations=["All India"])]),
    )
    jobs = ShineScraper("any").fetch()
    assert jobs[0].country_iso == "IN"
    assert jobs[0].location == "All India"


def test_city_list_joins_and_keeps_in(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(locations=["Bangalore", "Pune"])]),
    )
    jobs = ShineScraper("any").fetch()
    assert jobs[0].location == "Bangalore, Pune"
    assert jobs[0].country_iso == "IN"


def test_non_india_location_leaves_country_iso_blank(httpx_mock) -> None:
    """Shine carries occasional Gulf / SEA listings (``Dubai``,
    ``Singapore``). Don't claim ``IN`` for those — leave it None so
    downstream enrichment can fix."""
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(locations=["Dubai"])]),
    )
    jobs = ShineScraper("any").fetch()
    assert jobs[0].location == "Dubai"
    assert jobs[0].country_iso is None


def test_empty_location_list_yields_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(locations=[])]),
    )
    jobs = ShineScraper("any").fetch()
    assert jobs[0].location is None
    assert jobs[0].country_iso is None


# --- field parsing edge cases -----------------------------------------------


def test_experience_range_parses_min_and_max(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([
            _result(job_id=1, experience="3 to 7 Yrs"),
            _result(job_id=2, experience="15 to 19 Yrs"),
        ]),
    )
    jobs = sorted(ShineScraper("any").fetch(), key=lambda j: j.ats_id)
    assert jobs[0].experience == 3
    assert jobs[0].raw["experience_max"] == 7
    assert jobs[1].experience == 15
    assert jobs[1].raw["experience_max"] == 19


def test_experience_missing_leaves_none(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(experience="")]),
    )
    j = ShineScraper("any").fetch()[0]
    assert j.experience is None
    assert (j.raw or {}).get("experience_max") is None


def test_hidden_salary_dropped(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(salary="[Salary Hidden]")]),
    )
    j = ShineScraper("any").fetch()[0]
    assert j.salary_summary is None
    assert j.salary_currency is None


def test_explicit_salary_kept_as_summary(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(salary="6 to 12 Lakh")]),
    )
    j = ShineScraper("any").fetch()[0]
    assert j.salary_summary == "6 to 12 Lakh"


def test_skips_jobs_missing_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([
            _result(job_id=1, title="Good"),
            {"id": 2, "jCName": "X"},  # no title
            {"jJT": "No id", "jCName": "X"},  # no id
        ]),
    )
    jobs = ShineScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_fallback_url_when_slug_missing(httpx_mock) -> None:
    """Defensive: if ``jSlug`` is ever absent or empty, build the
    URL from the bare id so we still emit a usable Job."""
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_page([_result(job_id=99, slug="")]),
    )
    j = ShineScraper("any").fetch()[0]
    assert str(j.url) == "https://www.shine.com/jobs/99"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        ShineScraper("any").fetch()


def test_missing_next_data_returns_empty(httpx_mock) -> None:
    """If Shine ever serves a listing without the embedded payload
    (or a Cloudflare interstitial), we should yield zero jobs rather
    than blow up the entire fetch."""
    httpx_mock.add_response(
        url=_LISTING_RE,
        html="<html><body>no payload here</body></html>",
    )
    jobs = ShineScraper("any").fetch()
    assert jobs == []
