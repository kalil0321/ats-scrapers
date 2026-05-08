"""Tests for the dataset publisher.

The publisher is the most consequential piece of code — a buggy publish
silently produces a bad manifest that downstream clients will trust. We test
file layout, manifest structure, ordering (manifest last!), cache headers,
sha256 stability, and edge cases.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from jobhive.exceptions import StorageError
from jobhive.storage.publisher import (
    CACHE_CONTROL_DATED,
    CACHE_CONTROL_LATEST,
    DEFAULT_PREFIX,
    DatasetPublisher,
    _extract_date_from_filename,
)

# --- Layout & content --------------------------------------------------------

def test_publish_uploads_per_ats_and_full_snapshot(ats_csv_dir, fake_r2) -> None:
    """`all` is parquet-only; per-ATS slices ship CSV+parquet."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir)

    assert result.total_jobs == 9
    assert "jobhive/v1/manifest.json" in fake_r2.uploads
    assert "jobhive/v1/jobs/all.parquet" in fake_r2.uploads
    assert "jobhive/v1/jobs/all.csv" not in fake_r2.uploads  # parquet-only
    for ats in ("greenhouse", "lever", "ashby"):
        assert f"jobhive/v1/jobs/by-ats/{ats}.csv" in fake_r2.uploads
        assert f"jobhive/v1/jobs/by-ats/{ats}.parquet" in fake_r2.uploads


def test_companies_master_is_csv_only(ats_csv_dir, fake_r2, tmp_path: Path) -> None:
    """Companies are published as CSV (no parquet) at companies/all + per-ATS."""
    # Provide a companies file the publisher can read for greenhouse
    gh_companies = ats_csv_dir / "greenhouse" / "greenhouse_companies.csv"
    pd.DataFrame([{"name": "co0", "url": "https://example.com/co0"}]).to_csv(
        gh_companies, index=False
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    assert "jobhive/v1/companies/all.csv" in fake_r2.uploads
    assert "jobhive/v1/companies/all.parquet" not in fake_r2.uploads
    assert "jobhive/v1/companies/by-ats/greenhouse.csv" in fake_r2.uploads


# --- Manifest ----------------------------------------------------------------

def test_manifest_contains_expected_structure(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["stats"]["total_jobs"] == 9
    assert manifest["stats"]["ats_count"] == 3
    assert "greenhouse" in manifest["by_ats"]
    assert manifest["by_ats"]["greenhouse"]["rows"] == 3


def test_manifest_includes_generator_string(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["generator"].startswith("jobhive/")


def test_manifest_records_sha256_per_file(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    gh = manifest["by_ats"]["greenhouse"]
    assert "sha256" in gh
    assert len(gh["sha256"]) == 64  # hex sha256


def test_manifest_uses_public_urls_when_base_set(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["all"]["parquet"].startswith("https://cdn.example.com/")


def test_manifest_falls_back_to_keys_when_no_public_url(
    ats_csv_dir, fake_r2_no_public
) -> None:
    publisher = DatasetPublisher(fake_r2_no_public, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2_no_public.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["all"]["parquet"] == "jobhive/v1/jobs/all.parquet"


def test_manifest_includes_schema_version_and_columns(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["stats"]["schema_version"] == "2.0"
    assert "schema_columns" in manifest["stats"]


# --- Cache headers -----------------------------------------------------------

def test_cache_control_short_for_latest_files(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/jobs/all.parquet"]["cache_control"]
        == CACHE_CONTROL_LATEST
    )
    assert (
        fake_r2.uploads["jobhive/v1/manifest.json"]["cache_control"]
        == CACHE_CONTROL_LATEST
    )


def test_cache_control_immutable_for_dated_snapshots(
    ats_csv_dir, fake_r2, tmp_path: Path
) -> None:
    snap = tmp_path / "ai-03-05-2026.csv"
    pd.DataFrame([{"url": "https://x.com/1", "title": "x", "ats_type": "ashby"}]).to_csv(
        snap, index=False
    )
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir, dated_snapshots=[snap])
    assert (
        fake_r2.uploads["jobhive/v1/jobs/by-date/2026-05-03.parquet"]["cache_control"]
        == CACHE_CONTROL_DATED
    )


def test_per_ats_csv_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/jobs/by-ats/greenhouse.csv"]["content_type"]
        == "text/csv"
    )


def test_parquet_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/jobs/all.parquet"]["content_type"]
        == "application/vnd.apache.parquet"
    )


def test_manifest_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert fake_r2.uploads["jobhive/v1/manifest.json"]["content_type"] == "application/json"


# --- Ordering ----------------------------------------------------------------

def test_manifest_uploaded_after_data_files(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    keys = list(fake_r2.uploads.keys())
    assert keys.index("jobhive/v1/manifest.json") == len(keys) - 1


# --- Dated snapshots ---------------------------------------------------------

def test_dated_snapshots_become_by_date_entries(
    ats_csv_dir, fake_r2, tmp_path: Path
) -> None:
    snap = tmp_path / "ai-03-05-2026.csv"
    pd.DataFrame(
        [{"url": "https://x.com/1", "title": "x", "company": "y", "ats_type": "ashby"}]
    ).to_csv(snap, index=False)

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir, dated_snapshots=[snap])

    assert "jobhive/v1/jobs/by-date/2026-05-03.parquet" in fake_r2.uploads
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert "2026-05-03" in manifest["by_date"]


def test_dated_snapshot_with_unparseable_filename_is_skipped(
    ats_csv_dir, fake_r2, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snap = tmp_path / "no-date-here.csv"
    pd.DataFrame([{"url": "https://x.com/1", "title": "x", "ats_type": "ashby"}]).to_csv(
        snap, index=False
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    with caplog.at_level("WARNING"):
        publisher.publish_from_directory(ats_csv_dir, dated_snapshots=[snap])
    assert "no parseable date" in caplog.text


# --- Source preference -------------------------------------------------------

def test_when_dated_snapshot_provided_full_uses_its_schema(
    ats_csv_dir, fake_r2, tmp_path: Path
) -> None:
    """The richest dated snapshot should drive the `all` and `by-ats` slices."""
    snap = tmp_path / "ai-03-05-2026.csv"
    enriched = pd.DataFrame(
        [
            {
                "url": "https://x/1",
                "title": "Senior Engineer",
                "company": "acme",
                "ats_type": "greenhouse",
                "ats_id": "1",
                "location": "Remote",
                "salary_min": 100_000,
                "salary_max": 150_000,
                "experience": 3,
                "posted_at": "2026-04-01",
            }
        ]
    )
    enriched.to_csv(snap, index=False)
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir, dated_snapshots=[snap])

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert "salary_min" in manifest["stats"]["schema_columns"]
    assert "is_remote" in manifest["stats"]["schema_columns"]  # derived


# --- Filename date parser ----------------------------------------------------

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ai-03-05-2026.csv", "2026-05-03"),
        ("ai-30-04-2026.csv", "2026-04-30"),
        ("snapshot_2026-05-03.parquet", "2026-05-03"),
        ("data_03-05-2026_final.csv", "2026-05-03"),
    ],
)
def test_extract_date_handles_known_formats(filename: str, expected: str) -> None:
    assert _extract_date_from_filename(filename) == expected


@pytest.mark.parametrize("filename", ["random.csv", "no_date_here.parquet", "ai-may-2026.csv"])
def test_extract_date_returns_none_for_unparseable(filename: str) -> None:
    assert _extract_date_from_filename(filename) is None


# --- Custom prefix -----------------------------------------------------------

def test_custom_prefix_appears_in_all_keys(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, prefix="custom/v2", write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert all(k.startswith("custom/v2/") for k in fake_r2.uploads)


def test_default_prefix_is_jobhive_v1() -> None:
    assert DEFAULT_PREFIX == "jobhive/v1"


def test_prefix_strips_redundant_slashes(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, prefix="/foo/bar/", write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert "foo/bar/manifest.json" in fake_r2.uploads


# --- Error paths -------------------------------------------------------------

def test_publish_raises_when_no_csvs_present(tmp_path: Path, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    with pytest.raises(StorageError):
        publisher.publish_from_directory(tmp_path)


def test_publish_without_pyarrow_raises(monkeypatch, fake_r2) -> None:
    """When write_parquet=True but pyarrow is missing, we fail fast."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError("no pyarrow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(StorageError, match="pyarrow"):
        DatasetPublisher(fake_r2, write_parquet=True)


# --- Result object -----------------------------------------------------------

def test_result_reports_file_count_and_duration(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir)
    assert result.total_jobs == 9
    assert result.total_companies > 0
    assert result.duration_seconds >= 0.0
    assert result.manifest_key == "jobhive/v1/manifest.json"
    # at least: 3 ATS slices x 2 formats + 1 all parquet + 1 manifest = 8 files tracked
    assert len(result.files) >= 8


# --- Cross-ATS deduplication -------------------------------------------------

def test_cross_ats_dedup_collapses_mirror_listings(
    ats_csv_dir_with_duplicates, fake_r2
) -> None:
    """When the same (company, title, location) appears under multiple ATSes,
    the global ``all`` snapshot keeps one row. Per-ATS slices stay raw so
    consumers querying a single ATS see what that ATS actually exposes."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir_with_duplicates)

    # 3 jobs each in workday + eightfold = 6 raw, but cross-ATS dedup
    # collapses them to 3 unique (company, title, location) tuples.
    assert result.total_jobs == 3
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["stats"]["total_jobs"] == 3
    assert manifest["stats"]["total_jobs_raw"] == 6
    # Per-ATS slices are NOT deduped — both still report 3 rows.
    assert manifest["by_ats"]["workday"]["rows"] == 3
    assert manifest["by_ats"]["eightfold"]["rows"] == 3


def test_cross_ats_dedup_keeps_higher_priority_ats(
    ats_csv_dir_with_duplicates, fake_r2
) -> None:
    """Workday is priority 1; Eightfold is priority 5. The deduped ``all``
    snapshot must pick the Workday rows (URLs with workday.com)."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir_with_duplicates)

    all_parquet = fake_r2.uploads["jobhive/v1/jobs/all.parquet"]["data"]
    df = pd.read_parquet(pd.io.common.BytesIO(all_parquet))
    assert len(df) == 3
    # All surviving rows came from workday (priority 1).
    assert (df["ats_type"] == "workday").all()
    assert df["url"].str.contains("workday.com").all()


# --- SHA256 stability --------------------------------------------------------

def test_sha256_is_stable_across_runs(ats_csv_dir, fake_r2) -> None:
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    first = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    fake_r2.uploads.clear()
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    second = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    assert first["by_ats"]["greenhouse"]["sha256"] == second["by_ats"]["greenhouse"]["sha256"]


def test_sha256_matches_uploaded_bytes(ats_csv_dir, fake_r2) -> None:
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    csv_bytes = fake_r2.uploads["jobhive/v1/jobs/by-ats/greenhouse.csv"]["data"]
    assert hashlib.sha256(csv_bytes).hexdigest() == manifest["by_ats"]["greenhouse"]["sha256"]
