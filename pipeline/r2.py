"""Thin Cloudflare R2 client built on boto3's S3-compatible client.

Kept narrow on purpose: upload/copy/download/head/list. Anything more exotic
should go through boto3 directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ats_scrapers.exceptions import StorageConflictError, StorageError

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class R2Config:
    """Cloudflare R2 connection config.

    Either `endpoint` (full URL) or `account_id` is required — endpoint takes
    precedence so you can point the client at a custom-domain endpoint or a
    test bucket without touching the account-id-derived default.
    """

    bucket: str
    access_key_id: str
    secret_access_key: str
    account_id: str | None = None
    endpoint: str | None = None
    public_base_url: str | None = None

    @classmethod
    def from_env(cls, *, public_base_url: str | None = None) -> R2Config:
        """Build config from environment.

        Endpoint resolution order:
          1. `CLOUDFLARE_ENDPOINT`
          2. `ENDPOINT` (legacy short name)
          3. constructed from `CLOUDFLARE_ACCOUNT_ID`

        Required either way: bucket name, access key, secret key.
        """
        endpoint = os.getenv("CLOUDFLARE_ENDPOINT") or os.getenv("ENDPOINT")
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")

        if not endpoint and not account_id:
            raise StorageError(
                "Missing R2 endpoint configuration. "
                "Set CLOUDFLARE_ENDPOINT (preferred) or CLOUDFLARE_ACCOUNT_ID."
            )

        try:
            return cls(
                bucket=_required_env("CLOUDFLARE_BUCKET_NAME"),
                access_key_id=_required_env("CLOUDFLARE_ACCESS_KEY_ID"),
                secret_access_key=_required_env("CLOUDFLARE_SECRET_ACCESS_KEY"),
                account_id=account_id,
                endpoint=endpoint,
                public_base_url=public_base_url
                or os.getenv("CLOUDFLARE_PUBLIC_BASE_URL"),
            )
        except KeyError as exc:
            raise StorageError(str(exc)) from exc

    @property
    def endpoint_url(self) -> str:
        if self.endpoint:
            return self.endpoint
        if self.account_id:
            return f"https://{self.account_id}.r2.cloudflarestorage.com"
        raise StorageError("Either `endpoint` or `account_id` must be set on R2Config")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise KeyError(f"Missing required environment variable: {name}")
    return value


class R2Client:
    """Wrapper around boto3 S3 client configured for Cloudflare R2.

    Lazy-imports boto3 so the rest of ats-scrapers stays installable without it.
    """

    def __init__(self, config: R2Config) -> None:
        self._config = config
        try:
            import boto3
        except ImportError as exc:
            raise StorageError(
                "boto3 is required for R2 uploads. Install with `pip install -r pipeline/requirements.txt`."
            ) from exc

        self._client: Any = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name="auto",
        )

    @property
    def bucket(self) -> str:
        return self._config.bucket

    def upload(
        self,
        local_path: str | Path,
        key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        """Upload a file to R2 and return the object key."""
        local_path = Path(local_path)
        if not local_path.exists():
            raise StorageError(f"Local file not found: {local_path}")

        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control

        size = local_path.stat().st_size
        logger.info("Uploading %s (%d bytes) → r2://%s/%s", local_path, size, self.bucket, key)
        try:
            self._client.upload_file(str(local_path), self.bucket, key, ExtraArgs=extra or None)
        except Exception as exc:
            raise StorageError(f"R2 upload failed for {key}: {exc}") from exc
        return key

    def upload_bytes(
        self,
        data: bytes,
        key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        except Exception as exc:
            raise StorageError(f"R2 upload failed for {key}: {exc}") from exc
        return key

    def upload_bytes_if_current(
        self,
        data: bytes,
        key: str,
        *,
        expected_etag: str | None,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        from botocore.exceptions import ClientError

        extra: dict[str, Any] = {
            "IfMatch": expected_etag
        } if expected_etag is not None else {"IfNoneMatch": "*"}
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                **extra,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if status in {409, 412} or code in {
                "ConditionalRequestConflict",
                "PreconditionFailed",
            }:
                raise StorageConflictError(
                    f"R2 conditional write lost a race for {key}"
                ) from exc
            raise StorageError(f"R2 upload failed for {key}: {exc}") from exc
        except Exception as exc:
            raise StorageError(f"R2 upload failed for {key}: {exc}") from exc
        return key

    def copy(
        self,
        source_key: str,
        destination_key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        extra: dict[str, Any] = {
            "CopySource": {
                "Bucket": self.bucket,
                "Key": source_key,
            },
            "MetadataDirective": "REPLACE",
        }
        if content_type:
            extra["ContentType"] = content_type
        if cache_control:
            extra["CacheControl"] = cache_control
        try:
            self._client.copy_object(
                Bucket=self.bucket,
                Key=destination_key,
                **extra,
            )
        except Exception as exc:
            raise StorageError(
                f"R2 copy failed for {source_key} → "
                f"{destination_key}: {exc}"
            ) from exc
        return destination_key

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None

    def get_bytes(self, key: str) -> bytes | None:
        """Return the object body, or ``None`` if the key doesn't exist.

        Used by the publisher to read-modify-write the manifest without
        clobbering fields owned by another writer (the CI uploads the
        ``companies`` block; the publisher patches the ``by_ats`` jobs
        block; both must coexist in one manifest.json).
        """
        body, _etag = self.get_bytes_with_etag(key)
        return body

    def get_bytes_with_etag(
        self,
        key: str,
    ) -> tuple[bytes | None, str | None]:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None, None
            raise StorageError(f"R2 get failed for {key}: {exc}") from exc
        etag = response.get("ETag")
        return response["Body"].read(), etag if isinstance(etag, str) else None

    def list(self, prefix: str = "") -> Iterator[dict[str, Any]]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            yield from page.get("Contents", [])

    def delete(self, key: str) -> None:
        """Delete a single object."""
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"R2 delete failed for {key}: {exc}") from exc

    def delete_many(self, keys: list[str]) -> int:
        """Delete up to 1000 objects in one call. Returns the count deleted."""
        if not keys:
            return 0
        deleted = 0
        for batch_start in range(0, len(keys), 1000):
            batch = keys[batch_start : batch_start + 1000]
            try:
                response = self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except Exception as exc:
                raise StorageError(f"R2 batch delete failed: {exc}") from exc
            errors = response.get("Errors", [])
            if errors:
                details = ", ".join(
                    f"{error.get('Key', '<unknown>')}: "
                    f"{error.get('Code', 'unknown error')}"
                    for error in errors
                )
                raise StorageError(f"R2 batch delete was incomplete: {details}")
            deleted += len(batch)
        return deleted

    def public_url(self, key: str) -> str | None:
        if not self._config.public_base_url:
            return None
        return f"{self._config.public_base_url.rstrip('/')}/{key.lstrip('/')}"
