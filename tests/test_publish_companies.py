from __future__ import annotations

import io
import json
import runpy
from pathlib import Path

from botocore.exceptions import ClientError

SCRIPT = runpy.run_path(
    Path(__file__).parents[1] / ".github/scripts/publish_companies.py"
)
patch_manifest = SCRIPT["patch_manifest"]


class FakeClient:
    def __init__(
        self,
        manifest: dict[str, object] | None = None,
        *,
        conflict_once: dict[str, object] | None = None,
    ) -> None:
        self.manifest = manifest
        self.etag = '"etag-1"' if manifest is not None else None
        self.conflict_once = conflict_once
        self.puts: list[dict[str, object]] = []

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
        if self.manifest is None:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {
            "Body": io.BytesIO(json.dumps(self.manifest).encode()),
            "ETag": self.etag,
        }

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self.conflict_once is not None:
            self.manifest = self.conflict_once
            self.conflict_once = None
            self.etag = '"etag-2"'
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        self.manifest = json.loads(kwargs["Body"])
        self.etag = '"etag-3"'
        return {"ETag": self.etag}


def test_patch_manifest_preserves_jobs_fields() -> None:
    jobs = {"all": {"csv": "snapshot.csv"}, "by_ats": {"greenhouse": {}}}
    client = FakeClient({"version": "2.0", **jobs})

    result = patch_manifest(
        client,
        "bucket",
        aggregate_entry={"csv": "companies.csv", "rows": 2},
        by_ats_entries={"greenhouse": {"rows": 2}},
    )

    assert result["all"] == jobs["all"]
    assert result["by_ats"] == jobs["by_ats"]
    assert client.puts[0]["IfMatch"] == '"etag-1"'


def test_patch_manifest_retries_conflict_with_latest_jobs_fields() -> None:
    old_jobs = {"all": {"csv": "old.csv"}, "by_ats": {"greenhouse": {}}}
    new_jobs = {"all": {"csv": "new.csv"}, "by_ats": {"lever": {}}}
    client = FakeClient(
        {"version": "2.0", **old_jobs},
        conflict_once={"version": "2.0", **new_jobs},
    )

    result = patch_manifest(
        client,
        "bucket",
        aggregate_entry={"csv": "companies.csv", "rows": 2},
        by_ats_entries={"greenhouse": {"rows": 2}},
    )

    assert len(client.puts) == 2
    assert client.puts[1]["IfMatch"] == '"etag-2"'
    assert result["all"] == new_jobs["all"]
    assert result["by_ats"] == new_jobs["by_ats"]


def test_patch_manifest_uses_create_precondition() -> None:
    client = FakeClient()

    patch_manifest(
        client,
        "bucket",
        aggregate_entry={"csv": "companies.csv", "rows": 2},
        by_ats_entries={},
    )

    assert client.puts[0]["IfNoneMatch"] == "*"
