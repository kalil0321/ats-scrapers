"""Tests for the Google careers scraper.

Google's careers page has no JSON API and the visible CSS classes rotate,
so the scraper targets the stable ``aria-label="Learn more about ..."``
attribute Google attaches to job links. Pagination via ``?page=N``
terminates when a page yields no new IDs.

These tests pin:

1. aria-label-based parsing (title comes from aria, IDs from URL)
2. Pagination loops until a page returns no new IDs
3. URL canonicalization (strips query params)
4. Retry on 429/5xx
"""

from __future__ import annotations

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import GoogleScraper, ScraperRegistry


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.google as g
    monkeypatch.setattr(g, "MAX_RETRIES", 1)
    monkeypatch.setattr(g, "RETRY_BASE_DELAY", 0.0)


# Per-job detail enrichment fires HTML fetches against the canonical
# job URL after the listing pass; tests that mock only the listing
# pages tolerate the unmatched detail requests via this mark.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _page_url(page: int) -> str:
    if page == 1:
        return (
            "https://www.google.com/about/careers/applications/jobs/results"
            "?hl=en_US"
        )
    return (
        "https://www.google.com/about/careers/applications/jobs/results"
        f"?hl=en_US&page={page}"
    )


def _job_anchor(job_id: str, title: str, slug: str = "engineer") -> str:
    return (
        f'<a href="jobs/results/{job_id}-{slug}" '
        f'aria-label="Learn more about {title}">{title}</a>'
    )


def _page(anchors: list[str]) -> str:
    return f"<html><body>{''.join(anchors)}</body></html>"


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_google() -> None:
    assert ScraperRegistry.get(ATSType.GOOGLE) is GoogleScraper


# --- aria-label parsing -----------------------------------------------------


def test_uses_aria_label_for_title(httpx_mock) -> None:
    """The visible CSS classes change; only ``aria-label`` is stable."""
    httpx_mock.add_response(
        url=_page_url(1),
        text=_page([
            _job_anchor("142164974", "Cyber Threat Intelligence Analyst"),
        ]),
    )
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Cyber Threat Intelligence Analyst"
    assert jobs[0].ats_id == "142164974"
    assert jobs[0].company == "Google"


def test_skips_anchors_without_learn_more_aria_label(httpx_mock) -> None:
    """Google adds aria-labels to many other anchors (filters, navigation,
    etc.). Only those starting with 'Learn more about' are job links."""
    page = _page([
        _job_anchor("1", "Software Engineer"),
        '<a href="/foo" aria-label="Filter by location">Foo</a>',
        '<a href="/bar" aria-label="Apply to this job">Bar</a>',
    ])
    httpx_mock.add_response(url=_page_url(1), text=page)
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert [j.title for j in jobs] == ["Software Engineer"]


def test_dedupes_jobs_within_a_page(httpx_mock) -> None:
    """The Google page renders each job's anchor twice (once for the title
    link, once for an icon). Dedup by ID collapses them."""
    page = _page([
        _job_anchor("1", "Engineer"),
        _job_anchor("1", "Engineer"),
    ])
    httpx_mock.add_response(url=_page_url(1), text=page)
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert len(jobs) == 1


# --- URL canonicalization ---------------------------------------------------


def test_canonicalizes_url_strips_query_params(httpx_mock) -> None:
    """The ``href`` may carry tracking params (``?hl=en_US&_gl=...``); the
    canonical URL drops them so duplicate detection works across pages."""
    page = _page([
        '<a href="jobs/results/100-engineer?hl=en_US&_gl=foo" '
        'aria-label="Learn more about Engineer">Engineer</a>',
    ])
    httpx_mock.add_response(url=_page_url(1), text=page)
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert "?" not in str(jobs[0].url)
    assert str(jobs[0].url).endswith("/100-engineer")


# --- Pagination ------------------------------------------------------------


def test_paginates_until_no_new_ids(httpx_mock) -> None:
    """Three pages of unique jobs, page 4 returns the same IDs (no new)
    → terminate. Tests that pagination actually loops, not just page 1."""
    httpx_mock.add_response(
        url=_page_url(1),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(20)]),
    )
    httpx_mock.add_response(
        url=_page_url(2),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(20, 40)]),
    )
    httpx_mock.add_response(
        url=_page_url(3),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(40, 50)]),
    )
    # Page 4 returns the same jobs as page 3 (Google's listing rolled over).
    httpx_mock.add_response(
        url=_page_url(4),
        text=_page([_job_anchor(str(i), f"Job {i}") for i in range(40, 50)]),
    )
    jobs = GoogleScraper("google").fetch()
    assert len(jobs) == 50


def test_terminates_immediately_on_empty_first_page(httpx_mock) -> None:
    httpx_mock.add_response(url=_page_url(1), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert jobs == []


# --- Page 1 vs subsequent: page param shape ---------------------------------


def test_page_1_omits_page_param(httpx_mock) -> None:
    """Page 1 should NOT include ``page=1`` in the URL — the legacy and
    real Google career site treat that as redundant."""
    httpx_mock.add_response(
        url="https://www.google.com/about/careers/applications/jobs/results?hl=en_US",
        text=_page([_job_anchor("1", "X")]),
    )
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert len(jobs) == 1


# --- Error handling --------------------------------------------------------


def test_5xx_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.google as g
    monkeypatch.setattr(g, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_page_url(1), status_code=503)
    httpx_mock.add_response(
        url=_page_url(1), text=_page([_job_anchor("1", "Engineer")])
    )
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    jobs = GoogleScraper("google").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_is_honored(monkeypatch, httpx_mock) -> None:
    import asyncio

    import jobhive.scrapers.google as g
    monkeypatch.setattr(g, "MAX_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=_page_url(1), status_code=429, headers={"Retry-After": "12"}
    )
    httpx_mock.add_response(
        url=_page_url(1), text=_page([_job_anchor("1", "X")])
    )
    httpx_mock.add_response(url=_page_url(2), text=_page([]))
    GoogleScraper("google").fetch()
    assert 12.0 in sleeps


def test_5xx_exhausts_retries(monkeypatch, httpx_mock) -> None:
    import jobhive.scrapers.google as g
    monkeypatch.setattr(g, "MAX_RETRIES", 3)
    httpx_mock.add_response(url=_page_url(1), status_code=502, is_reusable=True)
    with pytest.raises(ScraperError, match="502"):
        GoogleScraper("google").fetch()
