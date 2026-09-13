"""Tests for the Meta scraper.

Scope: browser dependency gating, navigation failures, asynchronous response
capture and complete GraphQL listing validation. Real public endpoints are
probed separately without requiring credentials in the test suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers.meta import MetaScraper, _description_from_detail_html


def test_raises_when_cloakbrowser_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing browser must not erase the previous provider output."""
    from ats_scrapers.scrapers import _cloakbrowser

    def missing():
        raise ScraperError("cloakbrowser is required")
    monkeypatch.setattr(_cloakbrowser, "require_cloakbrowser", missing)
    with pytest.raises(ScraperError, match="cloakbrowser is required"):
        MetaScraper("meta").fetch()


def test_v2_listing_matches_separate_count_response() -> None:
    payloads = [
        {"data": {"job_search_with_featured_jobs_v2": {"job_count": 1}}},
        {"data": {"job_search_with_featured_jobs_v2": {
            "all_jobs": [{"id": "42", "title": "Engineer", "locations": ["London"]}],
            "featured_jobs": [{"id": "42", "title": "Engineer"}],
        }}},
    ]
    jobs = MetaScraper("meta")._validated_jobs(payloads)
    assert len(jobs) == 1
    assert jobs[0].ats_id == "42"


@pytest.mark.parametrize("count", [0, 2, -1, True, "1"])
def test_rejects_incomplete_or_invalid_total(count) -> None:
    payload = {"data": {"job_search_with_featured_jobs_v2": {
        "job_count": count, "all_jobs": [{"id": "42", "title": "Engineer"}],
    }}}
    with pytest.raises(ScraperError):
        MetaScraper("meta")._validated_jobs([payload])


@pytest.mark.parametrize("payloads", [[], [{}], [{"errors": [{"message": "blocked"}]}]])
def test_unknown_responses_do_not_become_successful_empty_jobs(payloads) -> None:
    with pytest.raises(ScraperError, match="refusing an empty result"):
        MetaScraper("meta")._validated_jobs(payloads)


def test_changed_total_during_capture_fails_closed() -> None:
    payloads = [
        {"data": {"job_search_with_featured_jobs_v2": {"job_count": 1}}},
        {"data": {"job_search_with_featured_jobs_v2": {
            "job_count": 2, "all_jobs": [{"id": "42", "title": "Engineer"}],
        }}},
    ]
    with pytest.raises(ScraperError, match="advertised totals"):
        MetaScraper("meta")._validated_jobs(payloads)


@pytest.mark.asyncio
async def test_navigation_failure_raises_and_closes_browser(monkeypatch) -> None:
    import cloakbrowser

    from ats_scrapers.scrapers import _cloakbrowser

    page = SimpleNamespace(on=lambda *args: None, goto=AsyncMock(side_effect=RuntimeError("tunnel failed")))
    browser = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(cloakbrowser, "launch_async", AsyncMock(return_value=browser))
    monkeypatch.setattr(_cloakbrowser, "evomi_proxy_from_env", lambda: None)
    with pytest.raises(ScraperError, match="tunnel failed"):
        await MetaScraper("meta")._fetch_via_cloakbrowser()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_capture_drains_v2_response_before_validation(monkeypatch) -> None:
    import cloakbrowser

    from ats_scrapers.scrapers import _cloakbrowser

    callbacks = {}
    payload = {"data": {"job_search_with_featured_jobs_v2": {
        "job_count": 1, "all_jobs": [{"id": "42", "title": "Engineer"}],
    }}}
    async def goto(*args, **kwargs):
        callbacks["response"](SimpleNamespace(url="https://www.metacareers.com/graphql", json=AsyncMock(return_value=payload)))
        return SimpleNamespace(status=200)
    async def settle(*args):
        await asyncio.sleep(0)
    page = SimpleNamespace(on=lambda event, callback: callbacks.update({event:callback}), goto=goto, wait_for_timeout=settle)
    browser = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(cloakbrowser, "launch_async", AsyncMock(return_value=browser))
    monkeypatch.setattr(_cloakbrowser, "evomi_proxy_from_env", lambda: None)
    jobs = await MetaScraper("meta",include_descriptions=False)._fetch_via_cloakbrowser()
    assert len(jobs) == 1
    browser.close.assert_awaited_once()


def test_meta_pipeline_keeps_previous_on_failed_or_empty_scrapes() -> None:
    from scripts.run_pipeline import CONFIGS

    assert CONFIGS["meta"]["fail_closed_on_any_error"] is True
    assert CONFIGS["meta"]["fail_closed_on_not_found"] is True
    assert CONFIGS["meta"]["fail_closed_on_empty"] is True


# --- GraphQL parsing -------------------------------------------------------


def test_parses_primary_response_shape() -> None:
    payload = {
        "data": {
            "job_search_with_featured_jobs": {
                "all_jobs": [
                    {
                        "id": "1234567890",
                        "title": "Software Engineer, Reality Labs",
                        "locations": ["Menlo Park, CA", "Seattle, WA"],
                        "teams": ["Engineering"],
                        "sub_teams": ["Reality Labs"],
                    }
                ]
            }
        }
    }
    [job] = MetaScraper("meta")._parse_responses([payload])
    assert job.ats_id == "1234567890"
    assert job.title == "Software Engineer, Reality Labs"
    assert str(job.url) == "https://www.metacareers.com/jobs/1234567890/"
    assert job.location == "Menlo Park, CA, Seattle, WA"
    assert job.team == "Engineering"
    assert job.department == "Reality Labs"


def test_dedupes_repeated_ids_across_responses() -> None:
    """Meta's UI fires the same query multiple times when the user
    interacts with filters; we mustn't double-count."""
    one = {
        "data": {
            "job_search_with_featured_jobs": {
                "all_jobs": [{"id": "1", "title": "Eng", "locations": ["NYC"]}]
            }
        }
    }
    [job] = MetaScraper("meta")._parse_responses([one, one, one])
    assert job.ats_id == "1"


def test_skips_entries_missing_id_or_title() -> None:
    payload = {
        "data": {
            "job_search_with_featured_jobs": {
                "all_jobs": [
                    {"id": "1", "title": "Has both"},
                    {"id": "2"},  # missing title
                    {"title": "Missing id"},
                    {},
                ]
            }
        }
    }
    jobs = MetaScraper("meta")._parse_responses([payload])
    assert {j.ats_id for j in jobs} == {"1"}


def test_falls_back_to_alternate_response_shape() -> None:
    """If Meta A/B-tests a different GraphQL alias we still pick up jobs."""
    payload = {
        "data": {
            "jobSearchResults": {
                "results": [{"id": "42", "title": "Researcher"}]
            }
        }
    }
    [job] = MetaScraper("meta")._parse_responses([payload])
    assert job.ats_id == "42"


def test_ignores_responses_without_data() -> None:
    """Some GraphQL payloads carry only an error envelope; they must
    not crash parsing."""
    assert MetaScraper("meta")._parse_responses(
        [{}, {"errors": [{"message": "rate limited"}]}]
    ) == []


def test_extracts_description_from_detail_json_ld() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "\\u0040context": "http://schema.org/",
        "\\u0040type": "JobPosting",
        "description": "Build Meta systems.",
        "responsibilities": "Operate global products."
      }
      </script>
    </head></html>
    """

    assert _description_from_detail_html(html) == (
        "Build Meta systems.\n\nOperate global products."
    )
