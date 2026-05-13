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


def test_publish_refuses_suspicious_empty_provider_slice(tmp_path, fake_r2) -> None:
    gh_dir = tmp_path / "greenhouse"
    gh_dir.mkdir()
    (gh_dir / "jobs.csv").write_text(
        "url,title,company,ats_type,ats_id,location,is_remote,salary_min,"
        "salary_max,salary_currency,salary_period,salary_summary,"
        "employment_type,department,team,description,posted_at,"
        "requisition_id,apply_url,commitment,raw\n",
        encoding="utf-8",
    )
    fake_r2.upload_bytes(
        json.dumps(
            {
                "version": "2.0",
                "by_ats": {"greenhouse": {"rows": 123, "size_bytes": 100}},
                "by_ats_companies": {"greenhouse": {"rows": 5, "size_bytes": 50}},
            }
        ).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    with pytest.raises(StorageError, match="Refusing to publish suspicious empty"):
        publisher.publish_from_directory(tmp_path)
    assert "jobhive/v1/greenhouse/jobs.csv" not in fake_r2.uploads


def test_publish_refuses_zero_byte_provider_slice_with_prior_manifest(
    tmp_path, fake_r2
) -> None:
    gh_dir = tmp_path / "greenhouse"
    gh_dir.mkdir()
    (gh_dir / "jobs.csv").write_bytes(b"")
    fake_r2.upload_bytes(
        json.dumps(
            {
                "version": "2.0",
                "by_ats": {"greenhouse": {"rows": 123, "size_bytes": 100}},
                "by_ats_companies": {"greenhouse": {"rows": 5, "size_bytes": 50}},
            }
        ).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    with pytest.raises(StorageError, match=r"local jobs\.csv is 0 bytes"):
        publisher.publish_from_directory(tmp_path)
    assert "jobhive/v1/greenhouse/jobs.csv" not in fake_r2.uploads


def test_publish_reuses_manifest_loaded_for_empty_slice_guard(
    ats_csv_dir, fake_r2
) -> None:
    fake_r2.upload_bytes(
        json.dumps(
            {
                "version": "2.0",
                "by_ats_companies": {"greenhouse": {"rows": 5}},
            }
        ).encode("utf-8"),
        "jobhive/v1/manifest.json",
        content_type="application/json",
    )
    calls = 0
    real_get_bytes = fake_r2.get_bytes

    def counted_get_bytes(key: str):
        nonlocal calls
        calls += 1
        return real_get_bytes(key)

    fake_r2.get_bytes = counted_get_bytes

    DatasetPublisher(fake_r2, write_parquet=True).publish_from_directory(ats_csv_dir)

    assert calls == 1


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


# --- Phase 1 / Phase 2 cross-source fuzzy dedup -----------------------------


@pytest.fixture
def ats_csv_dir_phase1(tmp_path):
    """Two aggregators emit the *same job* with formatting variations
    that defeat the exact-key (Pass 2) dedup:

      - eures: title with trailing Berufenet tag, location as NUTS
        prefix (``"DE (DEA58)"``).
      - bundesagentur: title without the tag, location as full text
        (``"Berlin, Berlin, Deutschland"``).

    Both rows share ``(company_norm, title_core, country_iso)`` so the
    new Phase 1 pass must collapse them; the global snapshot should
    keep only the higher-priority slice. Eures and Bundesagentur both
    sit at priority 6 — the earlier-emitted row (eures here, since the
    ATSType enum lists it first) wins on the tie-break.
    """
    # eures emits: title with trailing Berufenet code, NUTS-style location
    eures_dir = tmp_path / "eures"
    eures_dir.mkdir()
    pd.DataFrame([
        {
            "url": "https://eures.example/job/1",
            "title": "Backend Engineer (m/w/d) (Softwareentwickler/in)",
            "company": "ACME GmbH",
            "location": "DE (DE300)",
            "ats_id": "e1",
        },
        {
            "url": "https://eures.example/job/2",
            "title": "Marketing Manager (m/w/d) (Marketingfachkraft)",
            "company": "ACME GmbH",
            "location": "DE (DE712)",
            "ats_id": "e2",
        },
    ]).to_csv(eures_dir / "jobs.csv", index=False)

    # bundesagentur emits the same jobs without the Berufenet tag,
    # with full-text location.
    bundes_dir = tmp_path / "bundesagentur"
    bundes_dir.mkdir()
    pd.DataFrame([
        {
            "url": "https://arbeitsagentur.example/job/1",
            "title": "Backend Engineer (m/w/d)",
            "company": "ACME GmbH",
            "location": "Berlin, Berlin, Deutschland",
            "ats_id": "b1",
        },
        {
            "url": "https://arbeitsagentur.example/job/2",
            "title": "Marketing Manager (m/w/d)",
            "company": "ACME GmbH",
            "location": "München, Bayern, Deutschland",
            "ats_id": "b2",
        },
    ]).to_csv(bundes_dir / "jobs.csv", index=False)

    return tmp_path


def test_phase1_dedups_formatting_variations(ats_csv_dir_phase1, fake_r2):
    """Pass 4 (Phase 1) must collapse the eures / bundes mirror pair
    that differs only in trailing Berufenet tag + location format."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir_phase1)

    assert result.total_jobs_raw == 4  # two pairs of cross-source dups
    assert result.total_jobs == 2  # Phase 1 collapses each pair
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    # Per-ATS slices stay raw (2 rows each).
    assert manifest["by_ats"]["eures"]["rows"] == 2
    assert manifest["by_ats"]["bundesagentur"]["rows"] == 2


@pytest.fixture
def ats_csv_dir_phase2(tmp_path):
    """Two aggregators emit the same job with title typos / minor
    wording differences that defeat both the exact-key (Pass 2) and
    the formatting-normalised (Pass 4) dedups. Only Phase 2 fuzzy
    should collapse them.

    ``"Senior Backend Engineer (m/w/d)"`` vs
    ``"Sr. Backend Engineer (m/w/d)"`` — same role, ``token_set_ratio``
    sits around 86–95 depending on the rapidfuzz version. Phase 2's
    default threshold is 90 so this passes the bar.
    """
    eures_dir = tmp_path / "eures"
    eures_dir.mkdir()
    pd.DataFrame([{
        "url": "https://eures.example/fuzzy/1",
        "title": "Senior Backend Engineer (m/w/d)",
        "company": "Fuzzy GmbH",
        "location": "Berlin, Deutschland",
        "ats_id": "ef1",
    }]).to_csv(eures_dir / "jobs.csv", index=False)

    bundes_dir = tmp_path / "bundesagentur"
    bundes_dir.mkdir()
    pd.DataFrame([{
        "url": "https://arbeitsagentur.example/fuzzy/1",
        "title": "Senior Backend Engineer (m/w/d) - flexible",
        "company": "Fuzzy GmbH",
        "location": "München, Deutschland",
        "ats_id": "bf1",
    }]).to_csv(bundes_dir / "jobs.csv", index=False)

    return tmp_path


def test_phase2_fuzzy_dedups_title_variations(ats_csv_dir_phase2, fake_r2):
    """Pass 5 (Phase 2) must collapse cross-source rows whose titles
    differ by minor wording but share the ``(company_norm, country)``
    block."""
    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(ats_csv_dir_phase2)

    assert result.total_jobs_raw == 2
    assert result.total_jobs == 1


def test_phase2_does_not_cross_dedup_within_ats(tmp_path, fake_r2):
    """Two rows from the SAME ATS with near-identical titles must
    both survive — the publisher's contract is that per-ATS slices
    stay raw, and that contract must hold through fuzzy dedup too."""
    eures_dir = tmp_path / "eures"
    eures_dir.mkdir()
    pd.DataFrame([
        {
            "url": "https://eures.example/a",
            "title": "Senior Backend Engineer",
            "company": "Same GmbH",
            "location": "Berlin, Deutschland",
            "ats_id": "e1",
        },
        {
            "url": "https://eures.example/b",
            "title": "Sr. Backend Engineer",
            "company": "Same GmbH",
            "location": "Berlin, Deutschland",
            "ats_id": "e2",
        },
    ]).to_csv(eures_dir / "jobs.csv", index=False)

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    result = publisher.publish_from_directory(tmp_path)

    # Both within-ATS rows kept (fuzzy is cross-ATS-only).
    assert result.total_jobs == 2
    manifest = json.loads(fake_r2.uploads["jobhive/v1/manifest.json"]["data"])
    assert manifest["by_ats"]["eures"]["rows"] == 2


def test_phase2_respects_priority_when_dedupping(tmp_path, fake_r2):
    """When cross-ATS dups collide on fuzzy match, the higher-priority
    ATS's row wins. ``workday`` (priority 1) beats ``eightfold``
    (priority 5)."""
    workday_dir = tmp_path / "workday"
    workday_dir.mkdir()
    pd.DataFrame([{
        "url": "https://workday.example/job/1",
        "title": "Senior Backend Engineer (m/w/d)",
        "company": "Priority Co",
        "location": "Berlin, Deutschland",
        "ats_id": "w1",
    }]).to_csv(workday_dir / "jobs.csv", index=False)

    eightfold_dir = tmp_path / "eightfold"
    eightfold_dir.mkdir()
    pd.DataFrame([{
        "url": "https://eightfold.example/job/1",
        "title": "Sr. Backend Engineer (m/w/d)",
        "company": "Priority Co",
        "location": "Berlin, Deutschland",
        "ats_id": "ef1",
    }]).to_csv(eightfold_dir / "jobs.csv", index=False)

    publisher = DatasetPublisher(fake_r2, write_parquet=True)
    publisher.publish_from_directory(tmp_path)

    all_parquet = fake_r2.uploads["jobhive/v1/all.parquet"]["data"]
    df = pd.read_parquet(pd.io.common.BytesIO(all_parquet))
    assert len(df) == 1
    assert df.iloc[0]["ats_type"] == "workday"
    assert "workday.example" in df.iloc[0]["url"]


def test_phase2_oversize_block_skipped(caplog):
    """A block with more rows than ``fuzzy_max_block_size`` must be
    skipped (rather than blowing up the wall clock with n² fuzz
    calls); a warning is logged so the operator can investigate.

    Titles are made *distinct* between the two ATS slices so Phase 1
    (exact ``(company_norm, title_core, country)`` collapse) doesn't
    eat the rows before Phase 2 sees them — otherwise the oversize
    code path is never exercised.
    """
    import polars as pl

    from jobhive.storage.publisher import _decide_dedup_survivors_polars

    # 20 + 20 = 40-row block. Each ATS uses a different role family per
    # row so no two rows across slices share the same ``title_core``
    # (Phase 1 stays a no-op). The titles are still close enough that
    # rapidfuzz's ``token_set_ratio`` would fire if Phase 2 reached
    # them — which is exactly what the oversize guard prevents.
    keys_rows = []
    for i in range(20):
        keys_rows.append({
            "_local_idx": i, "_orig_idx": i,
            "_priority": 6, "ats_type": "eures",
            "url": f"https://eures.example/{i}",
            "title_raw": f"Backend Engineer Role {i:03d}",
            "title": f"backend engineer role {i:03d}",
            "company": "mega gmbh",
            "location": "berlin, deutschland",
            "ats_id": f"e{i}",
        })
    for i in range(20):
        keys_rows.append({
            "_local_idx": i, "_orig_idx": 20 + i,
            "_priority": 6, "ats_type": "bundesagentur",
            "url": f"https://arbeitsagentur.example/{i}",
            "title_raw": f"Senior Frontend Engineer Role {i:03d}",
            "title": f"senior frontend engineer role {i:03d}",
            "company": "mega gmbh",
            "location": "münchen, deutschland",
            "ats_id": f"b{i}",
        })
    keys = pl.DataFrame(keys_rows)

    with caplog.at_level("WARNING"):
        survivors = _decide_dedup_survivors_polars(
            keys, fuzzy_threshold=90, fuzzy_max_block_size=30,
        )

    # The warning is the contract: this block is skipped, not silently
    # dedupped past the cap.
    assert any(
        "Phase-2 fuzzy" in r.getMessage() and "oversize" in r.getMessage()
        for r in caplog.records
    ), f"expected oversize warning in {[r.getMessage() for r in caplog.records]}"

    # And the skip means nothing got dropped: all 40 rows survive
    # (eures 20 + bundesagentur 20).
    assert sum(s.height for s in survivors.values()) == 40


# --- helper-function unit tests ---------------------------------------------


def test_country_iso_extracts_common_eu_patterns():
    from jobhive.storage.publisher import _country_iso_from_location as f

    # Full-text suffixes (Bundesagentur style)
    assert f("Berlin, Berlin, Deutschland") == "DE"
    assert f("Paris, France") == "FR"
    assert f("Wien, Österreich") == "AT"
    assert f("Brussels, Belgium") == "BE"


def test_country_iso_uses_word_boundaries():
    """``"usa"`` is a substring of common European place names like
    ``"Lausanne"`` (CH). The earlier substring-match implementation
    tagged Lausanne jobs as US — cubic #1 on PR #33. The fix
    word-boundary-anchors every needle so the substring no longer
    matches."""
    from jobhive.storage.publisher import _country_iso_from_location as f

    # Lausanne (CH) standalone — no country suffix. The bare city name
    # used to false-positive on US via the ``usa`` substring; now it
    # returns empty until something else identifies the country.
    assert f("Lausanne") == ""
    assert f("Lausanne (Vaud)") == ""
    assert f("Lausanne, Vaud") == ""
    # With the country suffix the CH match wins because CH appears
    # before US in the patterns list.
    assert f("Lausanne, Suisse") == "CH"
    assert f("Lausanne, Switzerland") == "CH"
    # Other word-fragment false positives that used to fire:
    assert f("Glausage") == ""
    assert f("usable") == ""
    # Real US strings still match.
    assert f("New York, USA") == "US"
    assert f("U.S.A. office") == "US"

    # NUTS-prefix style (eures)
    assert f("DE (DEA58)") == "DE"
    assert f("FR (FRK21)") == "FR"

    # Mixed-case full text
    assert f("Zurich, Switzerland") == "CH"

    # No signal → empty
    assert f("") == ""
    assert f(None) == ""
    assert f("Remote") == ""


def test_title_core_strips_trailing_parenthesised_tag():
    from jobhive.storage.publisher import _title_core as f

    # The classic eures pattern: trailing Berufenet code in parens.
    assert (
        f("Anlagenmechaniker (m/w/d) ab 20€/Std. (Anlagenmechaniker/in)")
        == "anlagenmechaniker (m/w/d) ab 20€/std."
    )
    # Keep internal parens (m/w/d signals the same job).
    assert f("Marketing Manager (m/w/d)") == "marketing manager (m/w/d)"
    # Already clean title — no strip.
    assert f("Software Engineer") == "software engineer"
    # Empty / non-string
    assert f("") == ""
    assert f(None) == ""


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
