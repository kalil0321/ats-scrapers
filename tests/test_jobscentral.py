"""Tests for the JobsCentral SG (jobscentral.com.sg) scraper.

JobsCentral is a Next.js SSR site — the live job list ships embedded
in a ``<script id="__NEXT_DATA__">`` tag at
``props.pageProps.jobs.items``. These tests exercise:

1. ``__NEXT_DATA__`` extraction + parsing.
2. Mapping of JobsCentral's ``occupationType`` / ``remote`` /
   ``location`` shapes to the canonical schema.
3. URL composition using the per-category slug map (and the generic
   fallback when an unknown category enum shows up).
4. Pagination off ``pageProps.jobs.count`` + ``jobSearchModel.limit``.
5. Defensive parsing — missing __NEXT_DATA__ raises ScraperError.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JobsCentralScraper, ScraperRegistry, get_scraper

_LISTING_RE = re.compile(r"^https://jobscentral\.com\.sg/jobs")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jobscentral as jc
    monkeypatch.setattr(jc, "MAX_RETRIES", 1)
    monkeypatch.setattr(jc, "RETRY_BASE_DELAY", 0.0)


def _job(
    *,
    job_id: int = 1201537,
    title: str = "Sales Manager",
    company: str = "Aureus Group Pte Ltd",
    category: str = "SALES_BUSINESS_DEVELOPMENT_ACCOUNT_MANAGEMENT",
    section: str = "EDUCATION",
    occupation: str = "FULL_TIME",
    remote: str = "DISABLED",
    seniority: str | None = "MID_LEVEL",
    short_desc: str = "Lead and mentor a team of executives.",
    published_at: str = "2026-05-11T02:02:30.442Z",
    city: str = "Singapore",
    country: str = "Singapore",
    external_apply_url: str = "",
    status: str = "PUBLISHED",
    tags: list[dict] | None = None,
) -> dict:
    return {
        "id": job_id,
        "status": status,
        "createdAt": "2026-05-11T02:02:02.805Z",
        "publishedAt": published_at,
        "title": title,
        "shortDescription": short_desc,
        "occupationType": occupation,
        "remote": remote,
        "category": category,
        "seniority": seniority,
        "externalApplyUrl": external_apply_url,
        "score": 0.1,
        "requiredLanguages": [],
        "tags": tags if tags is not None else [
            {"id": 11, "name": "sales"},
            {"id": 12, "name": "team management"},
        ],
        "company": {
            "id": 1547, "name": company, "section": section,
            "slug": "aureus-group", "branding": "PREMIUM",
        },
        "location": {
            "id": 2, "city": city, "region": None,
            "prefacture": city, "country": country,
        },
        "expiresAt": "2026-06-10T02:02:30.442Z",
    }


def _next_data_html(
    items: list[dict],
    *,
    count: int | None = None,
    limit: int = 50,
) -> str:
    """Render a minimal HTML page with a ``__NEXT_DATA__`` script blob."""
    payload = {
        "props": {
            "pageProps": {
                "jobs": {
                    "count": count if count is not None else len(items),
                    "items": items,
                },
                "jobSearchModel": {
                    "title": None, "page": 0, "limit": limit,
                },
            },
        },
    }
    body = json.dumps(payload)
    return (
        "<!doctype html><html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{body}</script>'
        "</body></html>"
    )


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_jobscentral() -> None:
    assert ScraperRegistry.get(ATSType.JOBSCENTRAL_SG) is JobsCentralScraper


def test_get_scraper_returns_jobscentral() -> None:
    s = get_scraper("jobscentral_sg", "any")
    assert isinstance(s, JobsCentralScraper)


# --- Happy path -------------------------------------------------------------


def test_parses_basic_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job()]))
    jobs = JobsCentralScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_id == "1201537"
    assert j.ats_type is ATSType.JOBSCENTRAL_SG
    assert j.title == "Sales Manager"
    assert j.company == "Aureus Group Pte Ltd"
    assert j.country_iso == "SG"
    assert j.region == "Asia"
    assert j.location == "Singapore"
    assert j.language == "en"
    assert j.employment_type == "FULL_TIME"
    assert j.is_remote is False
    assert j.description == "Lead and mentor a team of executives."
    # URL uses the per-category slug.
    assert str(j.url) == (
        "https://jobscentral.com.sg/jobs/"
        "sales-or-business-development-or-account-management-jobs/1201537"
    )


# --- Employment type mapping ------------------------------------------------


@pytest.mark.parametrize(
    ("occupation", "expected"),
    [
        ("FULL_TIME", "FULL_TIME"),
        ("PART_TIME", "PART_TIME"),
        ("CONTRACT", "CONTRACT"),
        ("INTERNSHIP", "INTERN"),
        ("TEMPORARY", "TEMPORARY"),
        ("FREELANCE", "CONTRACT"),
        ("UNKNOWN_FUTURE_VALUE", None),
        ("", None),
    ],
)
def test_employment_type_mapping(
    httpx_mock, occupation: str, expected: str | None,
) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, html=_next_data_html([_job(occupation=occupation)]),
    )
    j = JobsCentralScraper("any").fetch()[0]
    assert j.employment_type == expected


# --- Remote tri-state -------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("ENABLED", True), ("DISABLED", False), ("OPTIONAL", None), ("", None)],
)
def test_remote_tristate(httpx_mock, flag: str, expected: bool | None) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE, html=_next_data_html([_job(remote=flag)]),
    )
    j = JobsCentralScraper("any").fetch()[0]
    assert j.is_remote is expected


# --- URL composition --------------------------------------------------------


def test_url_uses_known_category_slug(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        category="ENGINEERING", job_id=999,
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert str(j.url) == "https://jobscentral.com.sg/jobs/engineering-jobs/999"


def test_url_falls_back_for_unknown_category(httpx_mock) -> None:
    """Router accepts any slug — so we lowercase + ``-jobs`` for unknown
    enum values rather than fail the whole job."""
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        category="NEW_CATEGORY_2026", job_id=42,
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert str(j.url) == "https://jobscentral.com.sg/jobs/new_category_2026-jobs/42"


# --- Location formatting ----------------------------------------------------


def test_location_dedups_singapore_singapore(httpx_mock) -> None:
    """The structured location is {city: 'Singapore', country: 'Singapore'};
    naively joining produces 'Singapore, Singapore' which is ugly."""
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        city="Singapore", country="Singapore",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.location == "Singapore"


def test_location_combines_distinct_city_and_country(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        city="Jurong", country="Singapore",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.location == "Jurong, Singapore"


def test_country_iso_only_set_for_singapore(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        country="Malaysia",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.country_iso is None  # Domain is SG-only; foreign country = unmapped.


# --- Status filter ----------------------------------------------------------


def test_drops_non_published_rows(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([
        _job(job_id=1, status="PUBLISHED"),
        _job(job_id=2, status="EXPIRED"),
        _job(job_id=3, status="DRAFT"),
    ]))
    jobs = JobsCentralScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


# --- Apply URL --------------------------------------------------------------


def test_external_apply_url_when_http(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        external_apply_url="https://employer.com/apply/abc",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert str(j.apply_url) == "https://employer.com/apply/abc"


def test_external_apply_url_ignored_when_empty_or_invalid(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        external_apply_url="",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.apply_url is None


# --- Raw overflow -----------------------------------------------------------


def test_raw_captures_tags_seniority_category(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        tags=[{"id": 1, "name": "python"}, {"id": 2, "name": "django"}],
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["tags"] == ["python", "django"]
    assert j.raw["seniority_level"] == "MID_LEVEL"
    assert j.raw["category"] == "SALES_BUSINESS_DEVELOPMENT_ACCOUNT_MANAGEMENT"


# --- Pagination -------------------------------------------------------------


def test_paginates_when_count_exceeds_limit(httpx_mock) -> None:
    """When count > limit, fan out additional pages 1..N-1."""
    page0 = _next_data_html(
        [_job(job_id=i) for i in range(1, 51)], count=120, limit=50,
    )
    page1 = _next_data_html(
        [_job(job_id=i) for i in range(51, 101)], count=120, limit=50,
    )
    page2 = _next_data_html(
        [_job(job_id=i) for i in range(101, 121)], count=120, limit=50,
    )
    # Probe page=0 (no query) then page=1, page=2.
    httpx_mock.add_response(
        url="https://jobscentral.com.sg/jobs", html=page0,
    )
    httpx_mock.add_response(
        url="https://jobscentral.com.sg/jobs?page=1", html=page1,
    )
    httpx_mock.add_response(
        url="https://jobscentral.com.sg/jobs?page=2", html=page2,
    )
    jobs = JobsCentralScraper("any").fetch()
    assert len(jobs) == 120


def test_single_page_when_count_under_limit(httpx_mock) -> None:
    """The board is small — most fetches need only one page."""
    httpx_mock.add_response(
        url=_LISTING_RE,
        html=_next_data_html([_job(job_id=i) for i in range(1, 11)], count=10),
    )
    jobs = JobsCentralScraper("any").fetch()
    assert len(jobs) == 10
    assert len(httpx_mock.get_requests()) == 1


def test_max_pages_caps_fanout(httpx_mock) -> None:
    page0 = _next_data_html(
        [_job(job_id=i) for i in range(1, 51)], count=10_000, limit=50,
    )
    page1 = _next_data_html(
        [_job(job_id=i) for i in range(51, 101)], count=10_000, limit=50,
    )
    httpx_mock.add_response(url="https://jobscentral.com.sg/jobs", html=page0)
    httpx_mock.add_response(
        url="https://jobscentral.com.sg/jobs?page=1", html=page1,
    )
    # Page=2 must NOT be requested.
    jobs = JobsCentralScraper("any", max_pages=2).fetch()
    assert len(jobs) == 100


# --- Defensive --------------------------------------------------------------


def test_skips_listings_without_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([
        _job(job_id=1, title=""),
        {"id": "", "title": "no id", "status": "PUBLISHED"},
        _job(job_id=2, title="OK"),
    ]))
    jobs = JobsCentralScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["2"]


def test_dedupes_within_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([
        _job(job_id=99, title="A"),
        _job(job_id=99, title="A duplicate"),
    ]))
    jobs = JobsCentralScraper("any").fetch()
    assert len(jobs) == 1


def test_description_strips_html(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job(
        short_desc="<p>Build <strong>great</strong> things.</p>",
    )]))
    j = JobsCentralScraper("any").fetch()[0]
    assert j.description == "Build great things."


def test_description_truncated_to_10kb(httpx_mock) -> None:
    huge = "Lorem ipsum dolor sit amet. " * 800
    httpx_mock.add_response(
        url=_LISTING_RE, html=_next_data_html([_job(short_desc=huge)]),
    )
    j = JobsCentralScraper("any").fetch()[0]
    assert j.description is not None
    assert len(j.description) <= 10_000


def test_missing_next_data_raises(httpx_mock) -> None:
    """If JobsCentral ever switches to a non-Next.js page (or wraps the
    payload differently), surface a clean error so we notice in CI."""
    httpx_mock.add_response(
        url=_LISTING_RE, html="<html><body>no script</body></html>",
    )
    with pytest.raises(ScraperError, match="__NEXT_DATA__"):
        JobsCentralScraper("any").fetch()


def test_malformed_next_data_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_RE,
        html='<script id="__NEXT_DATA__">{not json}</script>',
    )
    with pytest.raises(ScraperError, match="not valid JSON"):
        JobsCentralScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobsCentralScraper("any").fetch()


def test_429_with_retry_after_is_honored(
    monkeypatch: pytest.MonkeyPatch, httpx_mock,
) -> None:
    import jobhive.scrapers.jobscentral as jc
    monkeypatch.setattr(jc, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=_LISTING_RE, status_code=429, headers={"Retry-After": "9"},
    )
    httpx_mock.add_response(url=_LISTING_RE, html=_next_data_html([_job()]))
    JobsCentralScraper("any").fetch()
    assert 9.0 in sleeps


def test_network_error_raises(httpx_mock) -> None:
    httpx_mock.add_exception(
        httpx.ConnectError("DNS failed"), url=_LISTING_RE, is_reusable=True,
    )
    with pytest.raises(ScraperError, match="DNS failed"):
        JobsCentralScraper("any").fetch()
