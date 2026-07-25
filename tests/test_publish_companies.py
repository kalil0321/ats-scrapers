"""Tests for the companies dataset publisher."""

from __future__ import annotations

import importlib.util
import json
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest
from botocore.exceptions import ClientError


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
            or (
                {
                    "by_ats_companies": {},
                    "stats": {"total_companies": 99},
                    "updated_at": "2000-01-01T00:00:00Z",
                },
                '"etag-1"',
            )
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
            assert kwargs["if_match"] == '"etag-1"'
            assert kwargs["if_none_match"] is None

    monkeypatch.setattr(module, "upload", record_upload)

    def record_alias_refresh(
        _client: object,
        _bucket: str,
        *,
        csv_key: str,
        csv_data: bytes,
        parquet_key: str,
        parquet_data: bytes,
    ) -> None:
        record_upload(
            _client,
            _bucket,
            csv_key,
            csv_data,
            "text/csv",
        )
        record_upload(
            _client,
            _bucket,
            parquet_key,
            parquet_data,
            "application/vnd.apache.parquet",
        )

    monkeypatch.setattr(
        module,
        "refresh_stable_aliases",
        record_alias_refresh,
    )
    monkeypatch.setattr(
        module,
        "delete_legacy",
        lambda _client, _bucket: None,
    )

    module.main()

    aggregate_csv_key = next(
        key for key in uploaded_keys
        if key.startswith(f"{module.PREFIX}/company-aggregates/")
        and key.endswith(".csv")
    )
    aggregate_parquet_key = next(
        key for key in uploaded_keys
        if key.startswith(f"{module.PREFIX}/company-aggregates/")
        and key.endswith(".parquet")
    )
    assert uploaded_keys == [
        f"{module.PREFIX}/greenhouse/companies.csv",
        aggregate_csv_key,
        aggregate_parquet_key,
        f"{module.PREFIX}/companies.csv",
        f"{module.PREFIX}/companies.parquet",
        f"{module.PREFIX}/manifest.json",
    ]
    assert operations == [
        *(f"upload:{key}" for key in uploaded_keys[:5]),
        "fetch_manifest",
        f"upload:{uploaded_keys[5]}",
    ]


class _AliasClient:
    def __init__(self, objects: dict[str, tuple[bytes, str, str | None]]) -> None:
        self.objects = objects

    def get_object(self, **kwargs: str) -> dict[str, object]:
        body, content_type, cache_control = self.objects[kwargs["Key"]]
        return {
            "Body": BytesIO(body),
            "ContentType": content_type,
            "CacheControl": cache_control,
        }

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        delete = kwargs["Delete"]
        assert isinstance(delete, dict)
        for item in delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}


def test_stable_alias_failure_rolls_back_both_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_publish_companies()
    csv_key = f"{module.PREFIX}/companies.csv"
    parquet_key = f"{module.PREFIX}/companies.parquet"
    old_csv = b"old csv"
    old_parquet = b"old parquet"
    new_csv = b"new csv"
    new_parquet = b"new parquet"
    client = _AliasClient({
        csv_key: (old_csv, "text/csv", module.CACHE_CONTROL_LATEST),
        parquet_key: (
            old_parquet,
            "application/vnd.apache.parquet",
            module.CACHE_CONTROL_LATEST,
        ),
    })

    def fail_new_parquet(
        _client: _AliasClient,
        _bucket: str,
        key: str,
        body: bytes,
        content_type: str,
        *,
        cache_control: str | None = None,
        **_kwargs: object,
    ) -> None:
        if key == parquet_key and body == new_parquet:
            raise ClientError(
                {"Error": {"Code": "InternalError"}},
                "PutObject",
            )
        client.objects[key] = (body, content_type, cache_control)

    monkeypatch.setattr(module, "upload", fail_new_parquet)

    with pytest.raises(ClientError):
        module.refresh_stable_aliases(
            client,
            "bucket",
            csv_key=csv_key,
            csv_data=new_csv,
            parquet_key=parquet_key,
            parquet_data=new_parquet,
        )

    assert client.objects[csv_key][0] == old_csv
    assert client.objects[parquet_key][0] == old_parquet


def test_manifest_patch_retries_after_concurrent_jobs_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_publish_companies()
    manifests = iter([
        (
            {
                "all": {"rows": 10},
                "by_ats": {"greenhouse": {"rows": 10}},
                "stats": {"total_jobs": 10, "total_companies": 1},
            },
            '"etag-1"',
        ),
        (
            {
                "all": {"rows": 12},
                "by_ats": {"greenhouse": {"rows": 12}},
                "stats": {"total_jobs": 12, "total_companies": 1},
            },
            '"etag-2"',
        ),
    ])
    monkeypatch.setattr(
        module,
        "fetch_existing_manifest",
        lambda _client, _bucket: next(manifests),
    )
    uploaded: list[tuple[dict[str, object], dict[str, object]]] = []

    def race_once(
        _client: object,
        _bucket: str,
        _key: str,
        body: bytes,
        _content_type: str,
        **kwargs: object,
    ) -> None:
        uploaded.append((json.loads(body), kwargs))
        if len(uploaded) == 1:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )

    monkeypatch.setattr(module, "upload", race_once)

    module.patch_manifest(
        object(),
        "bucket",
        aggregate_entry={"rows": 3},
        by_ats_entries={"greenhouse": {"rows": 3}},
        aggregate_rows=3,
    )

    assert len(uploaded) == 2
    final, kwargs = uploaded[-1]
    assert final["all"] == {"rows": 12}
    assert final["by_ats"] == {"greenhouse": {"rows": 12}}
    assert final["stats"] == {"total_jobs": 12, "total_companies": 3}
    assert kwargs["if_match"] == '"etag-2"'
