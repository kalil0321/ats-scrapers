"""Tests for the Meta scraper.

Scope: cloakbrowser gating + GraphQL parsing. The cloakbrowser /
Playwright path is exercised with live creds out-of-band — covering
it here would mean mocking Playwright's surface, which is more
brittle than useful.
"""

from __future__ import annotations

import logging

import pytest

from jobhive.scrapers.meta import MetaScraper


def test_returns_empty_with_warning_when_cloakbrowser_missing(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """When ``cloakbrowser`` isn't installed, the scraper degrades
    gracefully — logs a warning and returns ``[]`` so a publish run
    keeps moving."""
    from jobhive.scrapers import _cloakbrowser

    monkeypatch.setattr(_cloakbrowser, "is_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        jobs = MetaScraper("meta").fetch()
    assert jobs == []
    assert any("browser required" in r.getMessage().lower() for r in caplog.records)


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
