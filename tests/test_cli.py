"""Tests for the command-line entry points.

The dataset client and the scrapers are stubbed, so these tests check the
argument parsing and the JSON output without touching the network.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pandas as pd
import pytest

from ats_scrapers import cli
from ats_scrapers.exceptions import ManifestError, ScraperError
from ats_scrapers.models import ATSType, Job


def _job(ats_id: str = "123") -> Job:
    return Job(
        url=f"https://example.com/job/{ats_id}",
        title="Engineer",
        company="acme",
        ats_type=ATSType.GREENHOUSE,
        ats_id=ats_id,
    )


@pytest.fixture
def search_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_search(query: str | None = None, **kwargs: Any) -> pd.DataFrame:
        calls.append({"query": query, **kwargs})
        return pd.DataFrame([{"title": "Engineer", "company": "acme"}])

    monkeypatch.setattr("ats_scrapers.client.search", fake_search)
    return calls


def test_search_forwards_filters_and_prints_json(
    search_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    cli.search(
        ["--query", "engineer", "--location", "Paris", "--ats-type", "jobs_cz", "--limit", "5"]
    )

    assert search_calls == [
        {
            "query": "engineer",
            "location": "Paris",
            "company": None,
            "ats": "jobs_cz",
            "remote": None,
            "salary_min": None,
            "salary_max": None,
            "experience_max": None,
            "limit": 5,
        }
    ]
    assert json.loads(capsys.readouterr().out) == [{"title": "Engineer", "company": "acme"}]


def test_search_remote_is_tri_state(search_calls: list[dict[str, Any]]) -> None:
    cli.search([])
    cli.search(["--remote"])
    cli.search(["--no-remote"])

    assert [call["remote"] for call in search_calls] == [None, True, False]


def test_search_passes_any_source_name_to_the_dataset(search_calls: list[dict[str, Any]]) -> None:
    # A source the hosted dataset published after this release.
    cli.search(["--ats-type", "brand_new_source"])

    assert search_calls[0]["ats"] == "brand_new_source"


def test_search_reports_dataset_errors_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unknown_source(query: str | None = None, **kwargs: Any) -> pd.DataFrame:
        raise ManifestError("Unknown ATS 'nope'")

    monkeypatch.setattr("ats_scrapers.client.search", unknown_source)
    with pytest.raises(SystemExit) as exc:
        cli.search(["--ats-type", "nope"])
    assert exc.value.code == 1
    assert "Unknown ATS 'nope'" in capsys.readouterr().err


def test_search_options_require_a_value() -> None:
    with pytest.raises(SystemExit):
        cli.search(["--limit"])


def test_find_prints_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, int]] = []

    def fake_find(name: str, *, limit: int = 10) -> pd.DataFrame:
        seen.append((name, limit))
        return pd.DataFrame([{"ats": "greenhouse", "slug": "anthropic"}])

    monkeypatch.setattr("ats_scrapers.client.find_company", fake_find)
    cli.find(["anthropic", "--limit", "3"])

    assert seen == [("anthropic", 3)]
    assert json.loads(capsys.readouterr().out) == [{"ats": "greenhouse", "slug": "anthropic"}]


class _FakeScraper:
    instances: ClassVar[list[_FakeScraper]] = []

    def __init__(self, company_slug: str, *, include_descriptions: bool = True) -> None:
        self.company_slug = company_slug
        self.include_descriptions = include_descriptions
        _FakeScraper.instances.append(self)

    def fetch(self) -> list[Job]:
        return [_job("1"), _job("2")]


def test_fetch_prints_jobs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ats_scrapers.scrapers import ScraperRegistry

    _FakeScraper.instances = []
    monkeypatch.setattr(ScraperRegistry, "get", classmethod(lambda cls, ats: _FakeScraper))
    cli.fetch(["greenhouse", "anthropic", "--no-descriptions"])

    (scraper,) = _FakeScraper.instances
    assert scraper.company_slug == "anthropic"
    assert scraper.include_descriptions is False
    printed = json.loads(capsys.readouterr().out)
    assert [job["ats_id"] for job in printed] == ["1", "2"]


def test_fetch_rejects_a_source_without_a_scraper(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.fetch(["not-an-ats", "acme"])
    assert exc.value.code == 2
    assert "no scraper for 'not-an-ats'" in capsys.readouterr().err


def test_fetch_for_url_reports_unknown_urls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unknown(url: str, **kwargs: object) -> object:
        raise ScraperError(f"unrecognised careers URL: {url}")

    monkeypatch.setattr("ats_scrapers.resolve.get_scraper_for_url", unknown)
    with pytest.raises(SystemExit) as exc:
        cli.fetch_for_url(["https://example.com/careers"])
    assert exc.value.code == 1
    assert "unrecognised careers URL" in capsys.readouterr().err


def test_fetch_for_url_prints_jobs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "ats_scrapers.resolve.get_scraper_for_url",
        lambda url, **kwargs: _FakeScraper("acme", **kwargs),
    )
    cli.fetch_for_url(["https://boards.greenhouse.io/acme"])

    assert len(json.loads(capsys.readouterr().out)) == 2


def test_fetch_reports_a_bad_slug_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ats_scrapers.scrapers import ScraperRegistry

    def refuses(company_slug: str, **kwargs: object) -> object:
        raise ScraperError(f"expected a tenant slug, got {company_slug!r}")

    monkeypatch.setattr(ScraperRegistry, "get", classmethod(lambda cls, ats: refuses))
    with pytest.raises(SystemExit) as exc:
        cli.fetch(["greenhouse", "https://example.com/careers"])
    assert exc.value.code == 1
    assert "expected a tenant slug" in capsys.readouterr().err
