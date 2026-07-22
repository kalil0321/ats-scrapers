"""Tests for the companies directory lookup (`Client.find_company`)."""

from __future__ import annotations

import pandas as pd
import pytest

from ats_scrapers.client import Client
from ats_scrapers.exceptions import ManifestError

COMPANIES = pd.DataFrame(
    [
        {"ats": "ashby", "name": "OpenAI", "slug": "openai",
         "url": "https://jobs.ashbyhq.com/openai"},
        {"ats": "greenhouse", "name": "Anthropic", "slug": "anthropic",
         "url": "https://boards.greenhouse.io/anthropic"},
        {"ats": "lever", "name": "OpenAI Research Partners", "slug": "openai-research",
         "url": "https://jobs.lever.co/openai-research"},
        {"ats": "workday", "name": "Open Assistance Intl", "slug": None,
         "url": "https://oai.wd1.myworkdayjobs.com/jobs"},
    ]
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Client:
    c = Client()
    monkeypatch.setattr(Client, "companies", lambda self: COMPANIES.copy())
    return c


def test_exact_slug_match_ranks_first(client: Client) -> None:
    result = client.find_company("openai")
    assert not result.empty
    assert result.iloc[0]["slug"] == "openai"
    assert result.iloc[0]["ats"] == "ashby"
    # The partial match is still included, after the exact one.
    assert "openai-research" in set(result["slug"])


def test_name_match_is_case_insensitive(client: Client) -> None:
    result = client.find_company("ANTHROPIC")
    assert len(result) == 1
    assert result.iloc[0]["ats"] == "greenhouse"


def test_substring_matches_name_with_null_slug(client: Client) -> None:
    result = client.find_company("open assistance")
    assert len(result) == 1
    assert result.iloc[0]["ats"] == "workday"


def test_no_match_returns_empty(client: Client) -> None:
    assert client.find_company("definitely-not-a-company").empty


def test_blank_query_returns_empty(client: Client) -> None:
    assert client.find_company("   ").empty


def test_limit_is_applied(client: Client) -> None:
    assert len(client.find_company("o", limit=2)) == 2


def test_companies_download_uses_manifest_entry(
    monkeypatch: pytest.MonkeyPatch, httpx_mock,
) -> None:
    from ats_scrapers.manifest import FileEntry, Manifest, ManifestStats

    manifest = Manifest(
        version="1.0",
        generated_at="2026-07-22T00:00:00+00:00",
        stats=ManifestStats(total_jobs=1, total_companies=1, ats_count=1),
        all=FileEntry(csv="https://example.com/all.csv", rows=1, size_bytes=1),
        companies=FileEntry(
            csv="https://example.com/companies.csv", rows=1, size_bytes=1
        ),
    )
    httpx_mock.add_response(
        url="https://example.com/companies.csv",
        text="ats,name,slug,url\nashby,OpenAI,openai,https://jobs.ashbyhq.com/openai\n",
    )
    client = Client(prefer_parquet=False)
    monkeypatch.setattr(Client, "manifest", property(lambda self: manifest))
    df = client.companies()
    assert list(df.columns) == ["ats", "name", "slug", "url"]
    # Cached: second call must not re-download.
    client.companies()
    assert len(httpx_mock.get_requests()) == 1


def test_companies_without_manifest_entry_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ats_scrapers.manifest import FileEntry, Manifest, ManifestStats

    manifest = Manifest(
        version="1.0",
        generated_at="2026-07-22T00:00:00+00:00",
        stats=ManifestStats(total_jobs=1, total_companies=1, ats_count=1),
        all=FileEntry(csv="https://example.com/all.csv", rows=1, size_bytes=1),
    )
    client = Client()
    monkeypatch.setattr(Client, "manifest", property(lambda self: manifest))
    with pytest.raises(ManifestError, match="companies"):
        client.companies()
