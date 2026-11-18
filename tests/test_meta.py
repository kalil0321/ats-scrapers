"""Tests for the Meta scraper.

Scope: cloakbrowser gating + GraphQL parsing. The cloakbrowser /
Playwright path is exercised with live creds out-of-band — covering
it here would mean mocking Playwright's surface, which is more
brittle than useful.
"""

from __future__ import annotations

import json
import logging

import pytest

from ats_scrapers.scrapers.meta import MetaScraper, _description_from_detail_html


def test_returns_empty_with_warning_when_cloakbrowser_missing(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """When ``cloakbrowser`` isn't installed, the scraper degrades
    gracefully — logs a warning and returns ``[]`` so a publish run
    keeps moving."""
    from ats_scrapers.scrapers import _cloakbrowser

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


def test_parses_v2_response_shape() -> None:
    payload = {
        "data": {
            "job_search_with_featured_jobs_v2": {
                "all_jobs": [{"id": "42", "title": "Data Scientist", "locations": ["London, UK"]}]
            }
        }
    }
    [job] = MetaScraper("meta")._parse_responses([payload])
    assert job.ats_id == "42"
    assert job.location == "London, UK"


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


def test_encoded_jobposting_keeps_visible_punctuation() -> None:
    page = """<script type="application/ld+json">
      {&quot;@type&quot;:&quot;JobPosting&quot;,&quot;description&quot;:&quot;Use C#.&quot;}
    </script>"""
    assert _description_from_detail_html(page) == "Use C#."


@pytest.mark.parametrize("via_batch", [False, True])
def test_partial_graphql_is_enriched_from_structured_detail(httpx_mock, via_batch) -> None:
    entry = {"id": "123", "title": "Engineer", "description": "Intro"}
    scraper = MetaScraper("meta")
    [job] = scraper._parse_responses([{"data": {"careers": {"jobs": [entry]}}}])
    posting = {
        "@type": "JobPosting", "description": "Intro", "responsibilities": "Design.",
        "qualifications": ["Six years.", "Use &quot;SQL&quot;."],
    }
    httpx_mock.add_response(url=str(job.url), text=(
        '<script type="application/ld+json">' + json.dumps({"@graph": [posting]}) + '</script>'
    ))
    if via_batch:
        assert scraper.enrich_descriptions([job]) == [job]
    else:
        assert scraper.get_description(job) == job.description
    assert job.description == 'Intro\n\nDesign.\n\nSix years.\nUse "SQL".'
    scraper.get_description(job)
    assert len(httpx_mock.get_requests()) == 1


def test_complete_graphql_needs_no_detail_request() -> None:
    entry = {
        "id": "123", "title": "Engineer", "description": "Intro",
        "responsibilities": ["Build"], "minimum_qualifications": ["SQL"],
        "preferred_qualifications": ["C#"],
    }
    scraper = MetaScraper("meta")
    [job] = scraper._parse_responses([{"data": {"careers": {"jobs": [entry]}}}])
    assert scraper.get_description(job) == "Intro\n\nBuild\n\nSQL\n\nC#"


def test_partial_graphql_survives_failed_detail_read(httpx_mock) -> None:
    entry = {"id": "123", "title": "Engineer", "description": "Intro"}
    scraper = MetaScraper("meta")
    [job] = scraper._parse_responses([{"data": {"careers": {"jobs": [entry]}}}])
    httpx_mock.add_response(url=str(job.url), status_code=503)
    assert scraper.get_description(job) == "Intro"
