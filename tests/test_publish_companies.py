"""Tests for .github/scripts/publish_companies.py with an in-memory S3 client."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "publish_companies.py"
_spec = importlib.util.spec_from_file_location("publish_companies", _SCRIPT)
assert _spec is not None and _spec.loader is not None
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)

BASE = "https://storage.stapply.ai"


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    exceptions = SimpleNamespace(ClientError=_ClientError)

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)

    def put_object(self, **kw: Any) -> None:
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, **kw: Any) -> dict[str, Any]:
        if kw["Key"] not in self.objects:
            raise _ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[kw["Key"]])}

    def get_paginator(self, name: str) -> Any:
        keys = sorted(self.objects)

        def paginate(**kw: Any) -> list[dict[str, Any]]:
            return [{"Contents": [{"Key": k} for k in keys if k.startswith(kw["Prefix"])]}]

        return SimpleNamespace(paginate=paginate)

    def delete_objects(self, **kw: Any) -> None:
        for entry in kw["Delete"]["Objects"]:
            self.objects.pop(entry["Key"])

    def manifest(self, prefix: str) -> dict[str, Any]:
        return json.loads(self.objects[f"{prefix}/manifest.json"])


def _published_manifest() -> dict[str, Any]:
    return {
        "version": "2.0",
        "generated_at": "2026-11-17T14:30:00Z",
        "canonical_prefix": "jobs/v1",
        "deprecated_prefixes": {
            "jobhive/v1": {
                "replaced_by": "jobs/v1",
                "manifest": f"{BASE}/jobhive/v1/manifest.json",
            }
        },
        "stats": {"total_jobs": 10, "total_companies": 1, "ats_count": 1},
        "all": {"parquet": f"{BASE}/jobs/v1/all.parquet", "rows": 10, "size_bytes": 1},
        "companies": {"csv": f"{BASE}/jobhive/v1/companies.csv", "rows": 0, "size_bytes": 0},
    }


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, s3: _FakeS3) -> None:
    companies_dir = tmp_path / "ats-companies"
    companies_dir.mkdir()
    (companies_dir / "lever.csv").write_text(
        "name,slug,url\nAcme,acme,https://jobs.lever.co/acme\n"
    )
    monkeypatch.setattr(publish, "ATS_COMPANIES_DIR", companies_dir)
    monkeypatch.setattr(publish, "make_client", lambda: s3)
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", BASE)
    publish.main()


def test_publishes_to_both_prefixes_with_canonical_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = _published_manifest()
    raw = json.dumps(original).encode()
    s3 = _FakeS3(
        {
            "jobs/v1/manifest.json": raw,
            "jobhive/v1/manifest.json": raw,
            "jobhive/v1/companies/all.csv": b"legacy",
        }
    )
    _run(monkeypatch, tmp_path, s3)

    for prefix in ("jobs/v1", "jobhive/v1"):
        for name in ("lever/companies.csv", "companies.csv", "companies.parquet"):
            assert f"{prefix}/{name}" in s3.objects
        manifest = s3.manifest(prefix)
        assert manifest["companies"]["csv"] == f"{BASE}/jobs/v1/companies.csv"
        assert manifest["companies"]["parquet"] == f"{BASE}/jobs/v1/companies.parquet"
        assert manifest["by_ats_companies"]["lever"]["csv"] == (
            f"{BASE}/jobs/v1/lever/companies.csv"
        )
        assert manifest["by_ats_companies"]["lever"]["rows"] == 1
        for key in ("generated_at", "canonical_prefix", "deprecated_prefixes", "stats", "all"):
            assert manifest[key] == original[key]
    assert s3.manifest("jobs/v1") == s3.manifest("jobhive/v1")
    assert "jobhive/v1/companies/all.csv" not in s3.objects


def test_missing_manifest_is_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s3 = _FakeS3({"jobhive/v1/manifest.json": json.dumps(_published_manifest()).encode()})
    _run(monkeypatch, tmp_path, s3)

    assert "jobs/v1/manifest.json" not in s3.objects
    assert "jobs/v1/companies.csv" in s3.objects
    assert s3.manifest("jobhive/v1")["companies"]["csv"] == f"{BASE}/jobs/v1/companies.csv"


def test_patch_manifest_is_idempotent_and_leaves_input_alone() -> None:
    original = {**_published_manifest(), "version": "1.0", "companies_by_ats": {}}
    snapshot = copy.deepcopy(original)
    kwargs: dict[str, Any] = {
        "companies": {"csv": f"{BASE}/jobs/v1/companies.csv", "rows": 1, "size_bytes": 1},
        "by_ats_companies": {},
        "updated_at": "2026-11-18T00:00:00Z",
    }

    once = publish.patch_manifest(original, **kwargs)

    assert original == snapshot
    assert once["version"] == "2.0"
    assert "companies_by_ats" not in once
    assert publish.patch_manifest(once, **kwargs) == once
    assert publish.patch_manifest({**once, "version": "3.0"}, **kwargs)["version"] == "3.0"
