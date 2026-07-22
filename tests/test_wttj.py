"""Tests for the Welcome to the Jungle (Algolia-backed) scraper."""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import WTTJScraper

# httpx_mock matches by full URL, so define both variants we hit
SORTED_URL = re.compile(
    r"^https://csekhvms53-dsn\.algolia\.net/1/indexes/wttj_jobs_production_en_published_at_desc/query",
    re.IGNORECASE,
)
MAIN_URL = re.compile(
    r"^https://csekhvms53-dsn\.algolia\.net/1/indexes/wttj_jobs_production_en/query",
    re.IGNORECASE,
)


SAMPLE_HIT = {
    "objectID": "obj-1",
    "reference": "ref-1",
    "name": "Senior ML Engineer",
    "slug": "senior-ml-engineer-paris",
    "organization": {
        "name": "OpenAI France",
        "reference": "openai-fr",
        "slug": "openai",
    },
    "contract_type": "full_time",
    "experience_level_minimum": 3,
    "salary_minimum": 120000,
    "salary_maximum": 180000,
    "salary_currency": "EUR",
    "salary_period": "yearly",
    "salary_yearly_minimum": 120000,
    "remote": "partial",
    "has_remote": True,
    "_geoloc": [{"lat": 48.8566, "lng": 2.3522}],
    "offices": [{"city": "Paris", "state": "Île-de-France", "country": "France"}],
    "sectors": [{"name": "Software", "reference": "software"}],
    "key_missions": ["Build models", "Deploy to prod", "Mentor juniors"],
    "summary": "Senior ML role.",
    "published_at": "2026-04-15T10:00:00Z",
    "published_at_timestamp": 1744714800,
    "language": "en",
}


def test_wttj_per_org_parses_full_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        url=MAIN_URL,
        json={"hits": [SAMPLE_HIT], "nbHits": 1, "nbPages": 1},
    )
    jobs = WTTJScraper("openai", timeout=10).fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior ML Engineer"
    assert job.company == "OpenAI France"
    assert job.location == "Paris, Île-de-France, France"
    assert job.lat == pytest.approx(48.8566)
    assert job.lon == pytest.approx(2.3522)
    assert job.salary_min == 120_000
    assert job.salary_max == 180_000
    assert job.salary_currency == "EUR"
    assert job.salary_period == "YEAR"
    assert job.employment_type == "FULL_TIME"
    assert job.is_remote is True  # `partial` counts as remote-friendly
    assert job.experience == 3
    assert job.department == "Software"
    assert job.description and "Build models" in job.description


def test_wttj_honors_include_descriptions_false(httpx_mock) -> None:
    httpx_mock.add_response(
        url=MAIN_URL,
        json={"hits": [SAMPLE_HIT], "nbHits": 1, "nbPages": 1},
    )
    jobs = WTTJScraper("openai", include_descriptions=False).fetch()
    assert len(jobs) == 1
    assert jobs[0].description is None


def test_wttj_handles_fractional_experience(httpx_mock) -> None:
    """Regression: WTTJ returns experience as floats like 0.5."""
    hit = {**SAMPLE_HIT, "experience_level_minimum": 0.5}
    httpx_mock.add_response(url=MAIN_URL, json={"hits": [hit]})
    jobs = WTTJScraper("openai").fetch()
    assert jobs[0].experience == 0  # rounded to int


def test_wttj_full_walk_uses_sorted_replica(httpx_mock) -> None:
    """Full-platform walk must hit the `_published_at_desc` replica index."""
    httpx_mock.add_response(url=SORTED_URL, json={"hits": [SAMPLE_HIT]})
    httpx_mock.add_response(url=SORTED_URL, json={"hits": []})  # second page empty
    jobs = WTTJScraper("*", timeout=10).fetch()
    assert len(jobs) == 1


def test_wttj_dedupes_objects_returned_twice(httpx_mock) -> None:
    """Cursor walks may return the same hits when timestamps tie at boundary.

    The walk terminates as soon as a page returns no new objects.
    """
    hit_a = SAMPLE_HIT
    hit_b = {**SAMPLE_HIT, "objectID": "obj-2", "name": "Other"}
    httpx_mock.add_response(url=SORTED_URL, json={"hits": [hit_a, hit_b]})
    httpx_mock.add_response(url=SORTED_URL, json={"hits": [hit_b]})  # all duplicates
    jobs = WTTJScraper("*", timeout=10).fetch()
    assert len(jobs) == 2
    titles = {j.title for j in jobs}
    assert titles == {"Senior ML Engineer", "Other"}


def test_wttj_raises_on_403(httpx_mock) -> None:
    httpx_mock.add_response(url=MAIN_URL, status_code=403, text="forbidden")
    with pytest.raises(ScraperError, match="403"):
        WTTJScraper("openai").fetch()


def test_wttj_remote_value_mapping(httpx_mock) -> None:
    httpx_mock.add_response(
        url=MAIN_URL,
        json={
            "hits": [
                {**SAMPLE_HIT, "objectID": f"o{i}", "remote": v}
                for i, v in enumerate(["full", "partial", "no", "none"])
            ]
        },
    )
    jobs = WTTJScraper("openai").fetch()
    flags = [j.is_remote for j in jobs]
    assert flags == [True, True, False, False]


def test_wttj_in_registry() -> None:
    from ats_scrapers.scrapers import ScraperRegistry

    assert ScraperRegistry.get(ATSType.WELCOMETOTHEJUNGLE) is WTTJScraper
