"""Tests for the dataset publisher.

Covers the v2.0 layout:

    jobhive/v1/manifest.json
    jobhive/v1/all.parquet
    jobhive/v1/<ats>/jobs.{csv,parquet}

The publisher owns jobs entries in ``manifest.json``. Companies (top-level
``companies`` block + per-ATS ``by_ats_companies``) are written by the CI
workflow — the publisher must read-modify-write so those entries survive
each run.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from jobhive.exceptions import StorageError
from jobhive.storage.publisher import (
    CACHE_CONTROL_LATEST,
    DEFAULT_PREFIX,
    DatasetPublisher,
)

# --- Layout -----------------------------------------------------------------


def test_publish_writes_per_ats_and_full_snapshot(ats_csv_dir, fake_r2) -> None:
    """``all.{csv,parquet}`` live at the top level; per-ATS slices
    ship CSV+parquet under ``<ats>/jobs.{csv,parquet}``."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir)

    assert result.total_jobs == 9
    assert result.ats_count == 3
    assert "jobhive/v1/manifest.json" in fake_r2.uploads
    assert "jobhive/v1/all.parquet" in fake_r2.uploads
    assert "jobhive/v1/all.csv" in fake_r2.uploads
    for ats in ("greenhouse", "lever", "ashby"):
        assert f"jobhive/v1/{ats}/jobs.csv" in fake_r2.uploads
        assert f"jobhive/v1/{ats}/jobs.parquet" in fake_r2.uploads


def test_publisher_does_not_write_companies_anywhere(ats_csv_dir, fake_r2) -> None:
    """Companies are CI-owned. The publisher must never write them."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    company_keys = [k for k in fake_r2.uploads if "companies" in k]
    assert company_keys == []


def test_publisher_does_not_write_by_date(ats_csv_dir, fake_r2) -> None:
    """The v1 by-date paths are gone in v2."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    bydate_keys = [k for k in fake_r2.uploads if "/by-date/" in k]
    assert bydate_keys == []


# --- Manifest ---------------------------------------------------------------


def test_manifest_contains_expected_structure(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["version"] == "2.0"
    assert manifest["stats"]["total_jobs"] == 9
    assert manifest["stats"]["ats_count"] == 3
    assert "greenhouse" in manifest["by_ats"]
    assert manifest["by_ats"]["greenhouse"]["rows"] == 3
    # `all` lives at the top level now and ships both formats.
    assert manifest["all"]["parquet"].endswith("/all.parquet")
    assert manifest["all"]["csv"].endswith("/all.csv")


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
    assert len(gh["sha256"]) == 64
    assert "parquet_sha256" in gh
    assert len(gh["parquet_sha256"]) == 64


def test_manifest_uses_public_urls_when_base_set(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["all"]["parquet"].startswith("https://cdn.example.com/")
    assert manifest["by_ats"]["greenhouse"]["csv"].startswith(
        "https://cdn.example.com/"
    )


def test_manifest_falls_back_to_keys_when_no_public_url(
    ats_csv_dir, fake_r2_no_public
) -> None:
    publisher = DatasetPublisher(fake_r2_no_public, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(
        fake_r2_no_public.uploads["jobhive/v1/manifest.json"]["data"]
    )
    assert manifest["all"]["parquet"] == "jobhive/v1/all.parquet"
    assert manifest["by_ats"]["greenhouse"]["csv"] == "jobhive/v1/greenhouse/jobs.csv"


def test_manifest_includes_schema_version_and_columns(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["stats"]["schema_version"] == "2.0"
    assert "schema_columns" in manifest["stats"]


# --- Manifest patch (read-modify-write) -------------------------------------


def test_manifest_patch_preserves_companies_block(ats_csv_dir, fake_r2) -> None:
    """If the CI has previously uploaded a manifest with companies +
    by_ats_companies, the publisher must NOT clobber those keys."""
    pre_existing = {
        "version": "2.0",
        "companies": {
            "csv": "https://cdn.example.com/jobhive/v1/companies.csv",
            "parquet": "https://cdn.example.com/jobhive/v1/companies.parquet",
            "rows": 76627,
            "size_bytes": 4_990_244,
            "sha256": "a" * 64,
        },
        "by_ats_companies": {
            "greenhouse": {
                "csv": "https://cdn.example.com/jobhive/v1/greenhouse/companies.csv",
                "rows": 3076,
                "size_bytes": 176749,
                "sha256": "b" * 64,
            },
        },
        "updated_at": "2026-05-08T17:00:00Z",
    }
    fake_r2.upload_bytes(
        json.dumps(pre_existing).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["companies"] == pre_existing["companies"]
    assert manifest["by_ats_companies"] == pre_existing["by_ats_companies"]
    # And jobs entries got refreshed.
    assert manifest["by_ats"]["greenhouse"]["rows"] == 3
    assert manifest["all"]["parquet"].endswith("/all.parquet")


def test_manifest_patch_drops_legacy_fields(ats_csv_dir, fake_r2) -> None:
    """Pre-2.0 manifests carried `by_date` and `companies_by_ats`. Their
    underlying objects are deleted by `prune_legacy_paths`, so the
    manifest entries must be dropped too — leaving them would point
    consumers at 404s."""
    pre_existing = {
        "version": "1.0",
        "by_date": {"2026-05-03": {"parquet": "...", "rows": 50, "size_bytes": 60}},
        "companies_by_ats": {
            "greenhouse": {"csv": "...legacy...", "rows": 1, "size_bytes": 1}
        },
    }
    fake_r2.upload_bytes(
        json.dumps(pre_existing).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert "by_date" not in manifest
    assert "companies_by_ats" not in manifest


def test_manifest_patch_handles_missing_existing_manifest(ats_csv_dir, fake_r2) -> None:
    """First-ever publish has no manifest to read. Empty companies
    block in result is fine — the CI will fill it on next run."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["version"] == "2.0"
    assert "companies" not in manifest
    assert "by_ats_companies" not in manifest
    # Without a CI-written companies block, total_companies falls back
    # to 0 (instead of being absent — the published library 0.1.0
    # requires the field).
    assert manifest["stats"]["total_companies"] == 0


def test_total_companies_sums_by_ats_companies_rows(ats_csv_dir, fake_r2) -> None:
    """``stats.total_companies`` is derived from the CI's
    ``by_ats_companies`` block on every publish (not from a separate
    derivation), so the field stays fresh without re-uploading."""
    pre_existing = {
        "by_ats_companies": {
            "greenhouse": {"csv": "...", "rows": 3076, "size_bytes": 1, "sha256": "x" * 64},
            "lever": {"csv": "...", "rows": 1830, "size_bytes": 1, "sha256": "y" * 64},
            "ashby": {"csv": "...", "rows": 2058, "size_bytes": 1, "sha256": "z" * 64},
        },
    }
    fake_r2.upload_bytes(
        json.dumps(pre_existing).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["stats"]["total_companies"] == 3076 + 1830 + 2058


def test_manifest_patch_handles_corrupt_existing_manifest(
    ats_csv_dir, fake_r2, caplog
) -> None:
    """A non-JSON or non-object manifest must not crash the publish —
    we log a warning and proceed with a fresh manifest."""
    fake_r2.upload_bytes(
        b"<html>oops</html>",
        "jobhive/v1/manifest.json",
        content_type="text/html",
    )
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    with caplog.at_level("WARNING"):
        publisher.publish_from_directory(ats_csv_dir)
    assert "did not parse as JSON" in caplog.text
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["version"] == "2.0"


# --- Cache headers ----------------------------------------------------------


def test_cache_control_short_for_latest_files(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/all.parquet"]["cache_control"]
        == CACHE_CONTROL_LATEST
    )
    assert (
        fake_r2.uploads["jobhive/v1/manifest.json"]["cache_control"]
        == CACHE_CONTROL_LATEST
    )
    assert (
        fake_r2.uploads["jobhive/v1/greenhouse/jobs.csv"]["cache_control"]
        == CACHE_CONTROL_LATEST
    )


def test_per_ats_csv_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/greenhouse/jobs.csv"]["content_type"]
        == "text/csv"
    )


def test_parquet_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/all.parquet"]["content_type"]
        == "application/vnd.apache.parquet"
    )


def test_manifest_content_type(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    assert (
        fake_r2.uploads["jobhive/v1/manifest.json"]["content_type"]
        == "application/json"
    )


# --- Ordering ---------------------------------------------------------------


def test_manifest_uploaded_after_data_files(ats_csv_dir, fake_r2) -> None:
    """The manifest must be uploaded last — a half-finished publish must
    never expose a manifest pointing at missing files."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    keys = [
        k
        for k in fake_r2.uploads
        # Ignore the pre-existing manifest seeded by other tests'
        # paths through this fixture.
        if not k.endswith("/manifest.json")
    ]
    assert "jobhive/v1/manifest.json" in fake_r2.uploads
    last_manifest_index = max(
        i
        for i, k in enumerate(fake_r2.uploads)
        if k == "jobhive/v1/manifest.json"
    )
    last_data_index = max(
        i for i, k in enumerate(fake_r2.uploads) if k in keys
    )
    assert last_manifest_index > last_data_index


# --- Legacy cleanup ---------------------------------------------------------


def test_legacy_paths_pruned(ats_csv_dir, fake_r2) -> None:
    """Legacy (v1) keys must be removed when the publisher runs."""
    legacy_keys = [
        "jobhive/v1/jobs/all.parquet",
        "jobhive/v1/jobs/by-ats/greenhouse.csv",
        "jobhive/v1/jobs/by-ats/greenhouse.parquet",
        "jobhive/v1/jobs/by-date/2026-05-03.parquet",
        "jobhive/v1/companies/all.csv",
        "jobhive/v1/companies/by-ats/greenhouse.csv",
    ]
    for k in legacy_keys:
        fake_r2.upload_bytes(b"legacy", k, content_type="text/plain")

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)

    for k in legacy_keys:
        assert k not in fake_r2.uploads, f"legacy key still present: {k}"
        assert k in fake_r2.deleted, f"legacy key never marked deleted: {k}"


def test_prune_legacy_paths_is_idempotent(ats_csv_dir, fake_r2) -> None:
    """Running prune twice must not error and must report 0 the second
    time (nothing to delete)."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir)
    second_pass = publisher.prune_legacy_paths()
    assert second_pass == 0


# --- Custom prefix ----------------------------------------------------------


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


# --- Error paths ------------------------------------------------------------


def test_publish_raises_when_no_csvs_present(tmp_path, fake_r2) -> None:
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


# --- Result object ----------------------------------------------------------


def test_result_reports_counts_and_duration(ats_csv_dir, fake_r2) -> None:
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir)
    assert result.total_jobs == 9
    assert result.total_jobs_raw == 9  # no cross-ATS dups in this fixture
    assert result.ats_count == 3
    assert result.duration_seconds >= 0.0
    assert result.manifest_key == "jobhive/v1/manifest.json"
    # 3 ATS slices × 2 formats + all.{csv,parquet} + manifest.json = 9 files
    assert len(result.files) == 9


# --- Cross-ATS deduplication ------------------------------------------------


def test_cross_ats_dedup_collapses_mirror_listings(
    ats_csv_dir_with_duplicates, fake_r2
) -> None:
    """When the same (company, title, location) appears under multiple ATSes,
    the global ``all`` snapshot keeps one row. Per-ATS slices stay raw so
    consumers querying a single ATS see what that ATS actually exposes."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir_with_duplicates)

    assert result.total_jobs == 3
    assert result.total_jobs_raw == 6
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
    snapshot must keep the Workday rows."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(ats_csv_dir_with_duplicates)

    all_parquet = fake_r2.uploads["jobhive/v1/all.parquet"]["data"]
    df = pd.read_parquet(pd.io.common.BytesIO(all_parquet))
    assert len(df) == 3
    assert (df["ats_type"] == "workday").all()
    assert df["url"].str.contains("workday.com").all()


# --- SHA256 stability -------------------------------------------------------


def test_sha256_is_stable_across_runs(ats_csv_dir, fake_r2) -> None:
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    first = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    fake_r2.uploads.clear()
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    second = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    assert (
        first["by_ats"]["greenhouse"]["sha256"]
        == second["by_ats"]["greenhouse"]["sha256"]
    )


def test_sha256_matches_uploaded_bytes(ats_csv_dir, fake_r2) -> None:
    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])

    csv_bytes = fake_r2.uploads["jobhive/v1/greenhouse/jobs.csv"]["data"]
    assert (
        hashlib.sha256(csv_bytes).hexdigest()
        == manifest["by_ats"]["greenhouse"]["sha256"]
    )
