"""Tests for the Hosco scraper."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import HoscoScraper, ScraperRegistry
from jobhive.scrapers.hosco import _BUILD_ID_RE, BASE_URL

_JOBS_HTML_RE = re.compile(r"^https://www\.hosco\.com/en/jobs(?:\?|$)")
_NEXT_DATA_RE = re.compile(r"^https://www\.hosco\.com/_next/data/[^/]+/en/jobs\.json")

HOMEPAGE_HTML = """<!doctype html>
<html><head><title>Jobs</title></head>
<body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{},"page":"/jobs","query":{},"buildId":"TTaH8yf--XYgZKUfroyVa","isFallback":false}
</script>
</body></html>
"""


def _api_payload(results: list[dict], *, count: int) -> dict:
    return {
        "pageProps": {
            "initialState": {
                "jobDirectory": {
                    "search": {
                        "count": count,
                        "results": results,
                    }
                }
            }
        }
    }


JOB_ITEM_FULL = {
    "id": 5018462,
    "title": "Front Office Manager",
    "slug": "front-office-manager-acme-hotel-dubai",
    "url": "/en/jobs/front-office-manager-acme-hotel-dubai",
    "excerpt": "<p>Lead the <strong>front desk</strong> team in a 300-key property.</p>",
    "displayed_location": "Dubai, UAE",
    "company": {"id": 14021, "name": "Acme Hotel Group"},
    "owner": {"id": 9001, "type": "company"},
    "types": ["full-time"],
    "pay_range": {
        "currency": "AED",
        "min": 18000,
        "max": 22000,
        "period": "monthly",
    },
    "posted_date": "2026-05-10",
    "start_date": None,
    "cover_public_path": "/img/cover.png",
    "avatar": "/img/avatar.png",
}

JOB_ITEM_MIN = {
    "id": "abc-9912",
    "title": "Sommelier",
    "url": "/en/jobs/sommelier-london",
    "displayed_location": "London, United Kingdom",
    "company": {"name": "Maison Smith"},
    "excerpt": "Pair wine for a 2-Michelin-star tasting menu.",
    "types": ["Internship"],
    "posted_date": "2026-04-22",
}


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.hosco as h

    monkeypatch.setattr(h, "MAX_RETRIES", 1)
    monkeypatch.setattr(h, "RETRY_BASE_DELAY", 0.0)


# --- registry --------------------------------------------------------------


def test_registry_resolves_hosco() -> None:
    assert ScraperRegistry.get(ATSType.HOSCO) is HoscoScraper


def test_ats_type_value() -> None:
    assert ATSType.HOSCO.value == "hosco"


# --- buildId discovery -----------------------------------------------------


def test_build_id_regex_extracts_id_from_next_data() -> None:
    match = _BUILD_ID_RE.search(HOMEPAGE_HTML)
    assert match is not None
    assert match.group(1) == "TTaH8yf--XYgZKUfroyVa"


def test_build_id_regex_handles_minified_html() -> None:
    minified = (
        '<script>{"props":{"x":1},"buildId":"abc123-def","page":"/"}</script>'
    )
    match = _BUILD_ID_RE.search(minified)
    assert match is not None
    assert match.group(1) == "abc123-def"


def test_build_id_regex_missing_returns_none() -> None:
    assert _BUILD_ID_RE.search("<html><body>no next data here</body></html>") is None


# --- parsing ---------------------------------------------------------------


def test_parse_job_full_fields() -> None:
    scraper = HoscoScraper("any")
    job = scraper._parse_job(JOB_ITEM_FULL)
    assert job is not None
    assert job.ats_type is ATSType.HOSCO
    assert job.ats_id == "5018462"
    assert job.global_id == "hosco:5018462"
    assert job.title == "Front Office Manager"
    assert job.company == "Acme Hotel Group"
    assert (
        str(job.url)
        == f"{BASE_URL}/en/jobs/front-office-manager-acme-hotel-dubai"
    )
    assert job.location == "Dubai, UAE"
    assert job.salary_currency == "AED"
    assert job.salary_min == 18000
    assert job.salary_max == 22000
    # Period is inferred from the pay_range cadence key (no longer hardcoded
    # to YEAR); "monthly" maps to the canonical MONTH.
    assert job.salary_period == "MONTH"
    assert job.employment_type == "FULL_TIME"
    assert job.language == "en"
    assert job.posted_at is not None
    assert job.posted_at.year == 2026 and job.posted_at.month == 5
    # HTML should have been stripped out of the excerpt.
    assert job.description is not None
    assert "<p>" not in job.description
    assert "front desk" in job.description
    assert job.raw is not None
    assert job.raw["slug"] == "front-office-manager-acme-hotel-dubai"
    assert job.raw["owner_kind"] == "company"


def test_parse_job_minimal_no_salary_no_owner() -> None:
    scraper = HoscoScraper("any")
    job = scraper._parse_job(JOB_ITEM_MIN)
    assert job is not None
    assert job.ats_id == "abc-9912"
    assert job.company == "Maison Smith"
    assert job.salary_currency is None
    assert job.salary_min is None
    assert job.salary_max is None
    # No pay_range cadence to infer from — period stays None, not YEAR.
    assert job.salary_period is None
    assert job.employment_type == "INTERN"
    assert job.location == "London, United Kingdom"


def test_parse_job_missing_id_returns_none() -> None:
    scraper = HoscoScraper("any")
    assert scraper._parse_job({"title": "x", "url": "/foo"}) is None


def test_parse_job_missing_title_returns_none() -> None:
    scraper = HoscoScraper("any")
    assert scraper._parse_job({"id": 1, "url": "/foo"}) is None


def test_parse_job_drops_salary_when_currency_missing() -> None:
    """Min/max without a currency is meaningless — drop it rather than
    fabricating a currency."""
    scraper = HoscoScraper("any")
    item = dict(JOB_ITEM_FULL)
    item["pay_range"] = {"min": 18000, "max": 22000}
    job = scraper._parse_job(item)
    assert job is not None
    assert job.salary_currency is None
    assert job.salary_min is None
    assert job.salary_max is None


def test_parse_job_falls_back_to_unknown_company() -> None:
    scraper = HoscoScraper("any")
    item = dict(JOB_ITEM_FULL)
    item["company"] = {}
    job = scraper._parse_job(item)
    assert job is not None
    assert job.company == "Unknown"


# --- end-to-end fetch (httpx_mock) -----------------------------------------


def test_fetch_async_discovers_build_id_then_paginates(httpx_mock) -> None:
    httpx_mock.add_response(url=_JOBS_HTML_RE, text=HOMEPAGE_HTML)
    httpx_mock.add_response(
        url=_NEXT_DATA_RE,
        json=_api_payload([JOB_ITEM_FULL, JOB_ITEM_MIN], count=2),
    )

    jobs = HoscoScraper("any").fetch()
    assert len(jobs) == 2
    ids = {j.ats_id for j in jobs}
    assert ids == {"5018462", "abc-9912"}


def test_fetch_dedupes_repeated_ids(httpx_mock) -> None:
    httpx_mock.add_response(url=_JOBS_HTML_RE, text=HOMEPAGE_HTML)
    httpx_mock.add_response(
        url=_NEXT_DATA_RE,
        json=_api_payload([JOB_ITEM_FULL, JOB_ITEM_FULL], count=2),
    )
    jobs = HoscoScraper("any").fetch()
    assert len(jobs) == 1


def test_fetch_raises_when_build_id_missing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_JOBS_HTML_RE, text="<html><body>no next data</body></html>"
    )
    with pytest.raises(ScraperError, match="buildId"):
        HoscoScraper("any").fetch()


def test_fetch_raises_on_homepage_500(httpx_mock) -> None:
    httpx_mock.add_response(url=_JOBS_HTML_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        HoscoScraper("any").fetch()
