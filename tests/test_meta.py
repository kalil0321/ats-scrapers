"""Tests for the Meta scraper.

Scope: flag-gating + GraphQL parsing. The patchright browser path is
verified live (it needs a real browser binary), not mocked.
"""

from __future__ import annotations

import builtins
import logging

import pytest

from jobhive.exceptions import ScraperError
from jobhive.scrapers.meta import MetaScraper


@pytest.fixture(autouse=True)
def _clear_browserbase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "JOBHIVE_USE_BROWSERBASE",
        "JOBHIVE_DISABLE_BROWSERBASE",
        "BROWSERBASE_API_KEY",
        "BROWSERBASE_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def test_flag_off_returns_empty_with_warning(caplog) -> None:
    """Default (no flag) skips cleanly so a full pipeline keeps moving."""
    with caplog.at_level(logging.WARNING):
        jobs = MetaScraper("meta").fetch()
    assert jobs == []
    assert any("browser required" in r.getMessage().lower() for r in caplog.records)


def test_flag_on_without_patchright_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Meta now uses patchright (same as Tesla) instead of
    Browserbase. With the flag set but patchright missing, raise a
    clear install-instruction error."""
    monkeypatch.setenv("JOBHIVE_USE_BROWSERBASE", "1")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "patchright" or name.startswith("patchright."):
            raise ImportError("simulated: patchright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ScraperError, match="patchright"):
        MetaScraper("meta").fetch()


def test_disable_overrides_use_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill-switch wins even when the opt-in is set, matching Avature."""
    monkeypatch.setenv("JOBHIVE_USE_BROWSERBASE", "1")
    monkeypatch.setenv("JOBHIVE_DISABLE_BROWSERBASE", "1")
    assert MetaScraper("meta").fetch() == []


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
