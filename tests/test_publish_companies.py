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


class _Client:
    def __init__(self, errors: list[dict[str, str]] | None = None) -> None:
        self.deleted: list[str] = []
        self.errors = errors or []

    def delete_objects(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs["Bucket"] == "test-bucket"
        delete = kwargs["Delete"]
        assert isinstance(delete, dict)
        objects = delete["Objects"]
        assert isinstance(objects, list)
        self.deleted.extend(item["Key"] for item in objects)
        return {"Errors": self.errors}


def test_disabled_seek_company_artifact_is_deleted() -> None:
    module = _load_publish_companies()
    client = _Client()

    module.delete_disabled_sources(client, "test-bucket")

    assert client.deleted == ["jobhive/v1/seek/companies.csv"]


def test_disabled_seek_company_delete_failure_is_fatal() -> None:
    module = _load_publish_companies()
    client = _Client(
        errors=[
            {
                "Key": "jobhive/v1/seek/companies.csv",
                "Code": "AccessDenied",
            }
        ]
    )

    with pytest.raises(
        RuntimeError,
        match=r"seek/companies\.csv: AccessDenied",
    ):
        module.delete_disabled_sources(client, "test-bucket")
