"""Tests for the dataset manifest schema and fetcher."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ats_scrapers.exceptions import ManifestError
from ats_scrapers.manifest import Manifest, _pick_url
from ats_scrapers.models import ATSType


def test_manifest_parses_full_payload(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.stats.total_jobs == 1000
    assert "greenhouse" in m.by_ats
    assert m.by_ats["lever"].parquet is None
    assert "2026-05-03" in m.by_date


def test_manifest_default_version(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.version == "1.0"


def test_url_for_ats_prefers_parquet_when_available(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_ats(ATSType.GREENHOUSE) == "https://example.com/gh.parquet"


def test_url_for_ats_falls_back_to_csv(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_ats(ATSType.LEVER) == "https://example.com/lever.csv"


def test_url_for_ats_respects_prefer_parquet_false(
    sample_manifest_dict: dict[str, Any],
) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_ats(ATSType.GREENHOUSE, prefer_parquet=False) == "https://example.com/gh.csv"


def test_url_for_unknown_ats_raises(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    with pytest.raises(ManifestError, match="not present"):
        m.url_for_ats(ATSType.WORKDAY)


def test_manifest_accepts_source_without_local_scraper_enum(
    sample_manifest_dict: dict[str, Any],
) -> None:
    sample_manifest_dict["by_ats"]["beisen"] = {
        "csv": "https://example.com/beisen.csv",
        "rows": 10,
        "size_bytes": 100,
    }
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_ats("beisen") == "https://example.com/beisen.csv"


def test_url_for_all_returns_parquet_by_default(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_all() == "https://example.com/all.parquet"


def test_url_for_all_csv_fallback(sample_manifest_dict: dict[str, Any]) -> None:
    sample_manifest_dict["all"].pop("parquet")
    m = Manifest.model_validate(sample_manifest_dict)
    assert m.url_for_all() == "https://example.com/all.csv"


def test_pick_url_raises_when_neither_url_present() -> None:
    from ats_scrapers.manifest import FileEntry

    bad = FileEntry.model_construct(csv=None, parquet=None, rows=0, size_bytes=0)
    with pytest.raises(ManifestError):
        _pick_url(bad, prefer_parquet=True)


def test_pick_url_falls_back_to_parquet_when_csv_missing() -> None:
    from ats_scrapers.manifest import FileEntry

    entry = FileEntry(csv=None, parquet="https://x/y.parquet", rows=1, size_bytes=1)
    assert _pick_url(entry, prefer_parquet=False) == "https://x/y.parquet"


def test_fetch_handles_invalid_json(tmp_path) -> None:
    bad = tmp_path / "manifest.json"
    bad.write_text("not json")
    with pytest.raises(ManifestError):
        Manifest.fetch(f"file://{bad}")


def test_fetch_uses_provided_http_client(
    httpx_mock, sample_manifest_dict: dict[str, Any]
) -> None:
    httpx_mock.add_response(
        url="https://example.com/manifest.json",
        json=sample_manifest_dict,
    )
    with httpx.Client() as client:
        manifest = Manifest.fetch("https://example.com/manifest.json", client=client)
    assert manifest.stats.total_jobs == 1000


def test_fetch_raises_on_http_error(httpx_mock) -> None:
    httpx_mock.add_response(url="https://example.com/manifest.json", status_code=500)
    with pytest.raises(ManifestError):
        Manifest.fetch("https://example.com/manifest.json")


def test_fetch_raises_on_schema_mismatch(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://example.com/manifest.json",
        json={"version": "1.0"},  # missing required fields
    )
    with pytest.raises(ManifestError):
        Manifest.fetch("https://example.com/manifest.json")


def test_manifest_is_frozen(sample_manifest_dict: dict[str, Any]) -> None:
    from pydantic import ValidationError

    m = Manifest.model_validate(sample_manifest_dict)
    with pytest.raises(ValidationError):
        m.version = "2.0"  # type: ignore[misc]


def test_manifest_serializes_back_to_json(sample_manifest_dict: dict[str, Any]) -> None:
    m = Manifest.model_validate(sample_manifest_dict)
    payload = json.loads(m.model_dump_json())
    assert payload["stats"]["total_jobs"] == 1000
