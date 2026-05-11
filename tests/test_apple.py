"""Tests for Apple scraper retry / partial-result behaviour.

Apple's catalog is ~5 k jobs spread across ~250 paginated requests.
A single mid-fetch ``httpx.ReadTimeout`` (or transient 5xx) must not
discard the dozens of pages already accumulated — that was the
2026-05-11 regression where the cron yielded 0 rows instead of ~5 k.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import AppleScraper, ScraperRegistry
from jobhive.scrapers.apple import MAX_RETRIES

_CSRF_URL = "https://jobs.apple.com/api/v1/CSRFToken"
_SEARCH_URL = "https://jobs.apple.com/api/v1/search"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop sleep delay so retry tests run in <1 s instead of seconds."""
    import jobhive.scrapers.apple as m
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


def _csrf_mock(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_CSRF_URL,
        method="GET",
        headers={"x-apple-csrf-token": "tok-123"},
        json={},
    )


def _posting(req_id: str = "1", location: str = "Cupertino, California, United States") -> dict[str, Any]:
    return {
        "reqId": req_id,
        "positionId": req_id,
        "postingTitle": "Software Engineer",
        "transformedPostingTitle": "software-engineer",
        "jobSummary": "Build things.",
        "team": {"teamName": "Apple", "teamCode": "APL"},
        "postDateInGMT": "2026-05-01T10:00:00Z",
        "standardWeeklyHours": 40,
        "homeOffice": False,
        "locations": [{"name": location}],
    }


def _envelope(postings: list[dict], total: int | None = None) -> dict:
    return {
        "res": {
            "searchResults": postings,
            "totalRecords": total if total is not None else len(postings),
        }
    }


def test_registry_resolves_apple() -> None:
    assert ScraperRegistry.get(ATSType.APPLE) is AppleScraper


def test_transient_read_timeout_is_retried_to_success(httpx_mock) -> None:
    """Single ReadTimeout on first attempt must not abort — retry succeeds."""
    _csrf_mock(httpx_mock)
    httpx_mock.add_exception(httpx.ReadTimeout("boom"), url=_SEARCH_URL)
    httpx_mock.add_response(url=_SEARCH_URL, json=_envelope([_posting("a")], total=1))

    jobs = AppleScraper("apple").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"


def test_5xx_is_retried_to_success(httpx_mock) -> None:
    """Transient 503 (e.g. behind Cloudflare hiccup) must be retried."""
    _csrf_mock(httpx_mock)
    httpx_mock.add_response(url=_SEARCH_URL, status_code=503, text="upstream")
    httpx_mock.add_response(url=_SEARCH_URL, json=_envelope([_posting("b")], total=1))

    jobs = AppleScraper("apple").fetch()
    assert len(jobs) == 1


def test_retry_exhaustion_after_partial_returns_those_jobs(httpx_mock) -> None:
    """When all retries on page N (N≥2) are exhausted, return what was
    already accumulated from pages 1..N-1 instead of raising. This is
    the central fix: previously a single late timeout wiped the whole
    run."""
    _csrf_mock(httpx_mock)
    # Page 1: succeeds, full page (PAGE_SIZE=20 postings, totalRecords
    # tells the loop more pages exist).
    page1 = [_posting(str(i)) for i in range(20)]
    httpx_mock.add_response(url=_SEARCH_URL, json=_envelope(page1, total=40))
    # Page 2: timeout MAX_RETRIES times in a row. Use the constant so
    # this test does not drift if the retry budget changes.
    for _ in range(MAX_RETRIES):
        httpx_mock.add_exception(httpx.ReadTimeout("page 2 down"), url=_SEARCH_URL)

    jobs = AppleScraper("apple").fetch()
    # 20 rows from page 1 survived even though page 2 never landed.
    assert len(jobs) == 20


def test_page_1_exhaustion_raises_instead_of_masking_outage(httpx_mock) -> None:
    """When page 1 itself fails every retry, ``all_jobs`` is empty.
    Returning ``[]`` would let cron / downstream consumers treat a full
    outage as "Apple has no jobs today." Raise so the failure surfaces
    as a non-zero exit code."""
    _csrf_mock(httpx_mock)
    for _ in range(MAX_RETRIES):
        httpx_mock.add_exception(httpx.ReadTimeout("apple down"), url=_SEARCH_URL)

    with pytest.raises(ScraperError, match=r"page 1 failed after \d+ retries"):
        AppleScraper("apple").fetch()


def test_page_1_exhaustion_with_5xx_raises(httpx_mock) -> None:
    """Same invariant via 5xx exhaustion path rather than exception
    path — both must funnel to the same raise."""
    _csrf_mock(httpx_mock)
    for _ in range(MAX_RETRIES):
        httpx_mock.add_response(url=_SEARCH_URL, status_code=503, text="upstream")

    with pytest.raises(ScraperError, match=r"page 1 failed after \d+ retries"):
        AppleScraper("apple").fetch()


def test_non_retryable_4xx_still_raises(httpx_mock) -> None:
    """A 4xx other than 429 (e.g. 401 = stale CSRF, 400 = bad payload)
    must surface as ScraperError — retrying won't help, and silently
    breaking would mask an integration bug."""
    _csrf_mock(httpx_mock)
    httpx_mock.add_response(url=_SEARCH_URL, status_code=401, text="auth")

    with pytest.raises(ScraperError, match="returned 401"):
        AppleScraper("apple").fetch()


def test_429_is_retried(httpx_mock) -> None:
    _csrf_mock(httpx_mock)
    httpx_mock.add_response(url=_SEARCH_URL, status_code=429, text="slow down")
    httpx_mock.add_response(url=_SEARCH_URL, json=_envelope([_posting("c")], total=1))

    jobs = AppleScraper("apple").fetch()
    assert len(jobs) == 1
