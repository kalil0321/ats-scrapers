"""Tests for the companies dataset publisher."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_publish_companies() -> ModuleType:
    path = Path(".github/scripts/publish_companies.py")
    spec = importlib.util.spec_from_file_location("publish_companies", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disabled_company_artifact_is_left_unadvertised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_publish_companies()
    companies_dir = tmp_path / "ats-companies"
    companies_dir.mkdir()
    (companies_dir / "greenhouse.csv").write_text(
        "name,slug,url\nAcme,acme,https://example.com/jobs\n"
    )
    uploaded_keys: list[str] = []
    operations: list[str] = []

    monkeypatch.setattr(module, "ATS_COMPANIES_DIR", companies_dir)
    monkeypatch.setattr(module, "env", lambda _name: "test-bucket")
    monkeypatch.setattr(module, "make_client", object)
    monkeypatch.setattr(
        module,
        "fetch_existing_manifest",
        lambda _client, _bucket: (
            operations.append("fetch_manifest")
            or {
                "by_ats_companies": {},
                "stats": {"total_companies": 99},
                "updated_at": "2000-01-01T00:00:00Z",
            }
        ),
    )

    def record_upload(
        _client: object,
        _bucket: str,
        key: str,
        _body: bytes,
        _content_type: str,
        **kwargs: object,
    ) -> None:
        operations.append(f"upload:{key}")
        uploaded_keys.append(key)
        if key.endswith("/manifest.json"):
            assert kwargs["cache_control"] == module.CACHE_CONTROL_LATEST

    monkeypatch.setattr(module, "upload", record_upload)
    monkeypatch.setattr(
        module,
        "delete_legacy",
        lambda _client, _bucket: None,
    )

    module.main()

    assert uploaded_keys == [
        f"{module.PREFIX}/greenhouse/companies.csv",
        f"{module.PREFIX}/companies.csv",
        f"{module.PREFIX}/companies.parquet",
        f"{module.PREFIX}/manifest.json",
    ]
    assert operations == [
        "fetch_manifest",
        *(f"upload:{key}" for key in uploaded_keys),
    ]
