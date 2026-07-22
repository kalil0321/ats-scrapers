"""Tests for the Cloudflare R2 client wrapper and config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from ats_scrapers.exceptions import StorageError
from ats_scrapers.storage.r2 import R2Client, R2Config

R2_VARS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_ENDPOINT",
    "CLOUDFLARE_BUCKET_NAME",
    "CLOUDFLARE_ACCESS_KEY_ID",
    "CLOUDFLARE_SECRET_ACCESS_KEY",
    "CLOUDFLARE_PUBLIC_BASE_URL",
    "ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in R2_VARS:
        monkeypatch.delenv(var, raising=False)


# --- R2Config ----------------------------------------------------------------

def test_from_env_with_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("CLOUDFLARE_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("CLOUDFLARE_SECRET_ACCESS_KEY", "secret")
    config = R2Config.from_env()
    assert config.account_id == "acc123"
    assert config.endpoint_url == "https://acc123.r2.cloudflarestorage.com"


def test_from_env_with_explicit_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_ENDPOINT", "https://abc.r2.cloudflarestorage.com")
    monkeypatch.setenv("CLOUDFLARE_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("CLOUDFLARE_SECRET_ACCESS_KEY", "secret")
    config = R2Config.from_env()
    assert config.endpoint_url == "https://abc.r2.cloudflarestorage.com"


def test_from_env_with_short_endpoint_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINT", "https://abc.r2.cloudflarestorage.com")
    monkeypatch.setenv("CLOUDFLARE_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("CLOUDFLARE_SECRET_ACCESS_KEY", "secret")
    config = R2Config.from_env()
    assert config.endpoint_url == "https://abc.r2.cloudflarestorage.com"


def test_from_env_explicit_endpoint_wins_over_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("CLOUDFLARE_ENDPOINT", "https://override.example.com")
    monkeypatch.setenv("CLOUDFLARE_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("CLOUDFLARE_SECRET_ACCESS_KEY", "secret")
    config = R2Config.from_env()
    assert config.endpoint_url == "https://override.example.com"


def test_from_env_missing_endpoint_and_account_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("CLOUDFLARE_SECRET_ACCESS_KEY", "secret")
    with pytest.raises(StorageError, match="endpoint"):
        R2Config.from_env()


@pytest.mark.parametrize(
    "missing",
    ["CLOUDFLARE_BUCKET_NAME", "CLOUDFLARE_ACCESS_KEY_ID", "CLOUDFLARE_SECRET_ACCESS_KEY"],
)
def test_from_env_missing_required_var_raises(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    full = {
        "CLOUDFLARE_ACCOUNT_ID": "acc",
        "CLOUDFLARE_BUCKET_NAME": "bucket",
        "CLOUDFLARE_ACCESS_KEY_ID": "key",
        "CLOUDFLARE_SECRET_ACCESS_KEY": "secret",
    }
    for k, v in full.items():
        if k == missing:
            continue
        monkeypatch.setenv(k, v)
    with pytest.raises(StorageError, match=missing):
        R2Config.from_env()


def test_from_env_picks_up_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("CLOUDFLARE_ACCOUNT_ID", "acc"),
        ("CLOUDFLARE_BUCKET_NAME", "bucket"),
        ("CLOUDFLARE_ACCESS_KEY_ID", "key"),
        ("CLOUDFLARE_SECRET_ACCESS_KEY", "secret"),
        ("CLOUDFLARE_PUBLIC_BASE_URL", "https://cdn.example.com"),
    ]:
        monkeypatch.setenv(k, v)
    config = R2Config.from_env()
    assert config.public_base_url == "https://cdn.example.com"


def test_endpoint_url_uses_explicit_endpoint_field() -> None:
    config = R2Config(
        account_id=None,
        endpoint="https://custom.example.com",
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
    )
    assert config.endpoint_url == "https://custom.example.com"


def test_endpoint_url_constructed_from_account_id_when_endpoint_missing() -> None:
    config = R2Config(
        account_id="acc123",
        endpoint=None,
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
    )
    assert config.endpoint_url == "https://acc123.r2.cloudflarestorage.com"


# --- R2Client (uploads, lists, public urls) ----------------------------------

class FakeBoto3Client:
    """Captures boto3 calls without hitting AWS/R2."""

    def __init__(self) -> None:
        self.uploaded_files: list[tuple[str, str, str, dict]] = []
        self.put_objects: list[dict] = []
        self.head_responses: dict[str, dict] = {}

    def upload_file(self, src: str, bucket: str, key: str, ExtraArgs=None) -> None:  # noqa: N803
        self.uploaded_files.append((src, bucket, key, ExtraArgs or {}))

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **extra) -> None:  # noqa: N803
        self.put_objects.append({"Bucket": Bucket, "Key": Key, "Body": Body, **extra})

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803
        if Key in self.head_responses:
            return self.head_responses[Key]
        raise Exception("404")

    def get_paginator(self, _name: str):
        class P:
            def paginate(self, **kwargs):
                yield {"Contents": [{"Key": f"{kwargs.get('Prefix', '')}example"}]}

        return P()


@pytest.fixture
def fake_r2_client(monkeypatch: pytest.MonkeyPatch) -> tuple[R2Client, FakeBoto3Client]:
    fake = FakeBoto3Client()

    class FakeBoto3Module:
        @staticmethod
        def client(*args, **kwargs):
            return fake

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3Module())
    config = R2Config(
        account_id="acc",
        endpoint=None,
        bucket="bucket",
        access_key_id="key",
        secret_access_key="secret",
        public_base_url="https://cdn.example.com",
    )
    return R2Client(config), fake


def test_upload_calls_boto_with_extra_args(
    tmp_path: Path, fake_r2_client: tuple[R2Client, FakeBoto3Client]
) -> None:
    client, fake = fake_r2_client
    src = tmp_path / "x.csv"
    src.write_text("a,b\n1,2\n")
    client.upload(src, "jobhive/v1/x.csv", content_type="text/csv", cache_control="public")
    assert len(fake.uploaded_files) == 1
    assert fake.uploaded_files[0][2] == "jobhive/v1/x.csv"
    assert fake.uploaded_files[0][3] == {
        "ContentType": "text/csv",
        "CacheControl": "public",
    }


def test_upload_raises_when_local_file_missing(
    tmp_path: Path, fake_r2_client: tuple[R2Client, FakeBoto3Client]
) -> None:
    client, _ = fake_r2_client
    with pytest.raises(StorageError):
        client.upload(tmp_path / "missing.csv", "k")


def test_upload_bytes_calls_put_object(
    fake_r2_client: tuple[R2Client, FakeBoto3Client],
) -> None:
    client, fake = fake_r2_client
    client.upload_bytes(b"hello", "k", content_type="text/plain")
    assert fake.put_objects[0]["Body"] == b"hello"
    assert fake.put_objects[0]["Key"] == "k"


def test_public_url_returns_none_when_no_base() -> None:
    config = R2Config(
        account_id="acc",
        endpoint=None,
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
        public_base_url=None,
    )

    class FakeMod:
        @staticmethod
        def client(*args, **kwargs):
            return FakeBoto3Client()

    import sys

    sys.modules["boto3"] = FakeMod()  # type: ignore[assignment]
    client = R2Client(config)
    assert client.public_url("any/key") is None


def test_public_url_strips_redundant_slashes(
    fake_r2_client: tuple[R2Client, FakeBoto3Client],
) -> None:
    client, _ = fake_r2_client
    assert client.public_url("/jobhive/v1/x.csv") == "https://cdn.example.com/jobhive/v1/x.csv"


def test_head_returns_none_when_object_missing(
    fake_r2_client: tuple[R2Client, FakeBoto3Client],
) -> None:
    client, _ = fake_r2_client
    assert client.head("missing/key") is None


def test_list_iterates_paginated_results(
    fake_r2_client: tuple[R2Client, FakeBoto3Client],
) -> None:
    client, _ = fake_r2_client
    results = list(client.list(prefix="jobhive/"))
    assert results[0]["Key"] == "jobhive/example"
