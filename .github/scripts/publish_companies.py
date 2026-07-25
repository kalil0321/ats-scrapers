"""Publish ats-companies/ to Cloudflare R2 + patch the manifest.

Triggered by `.github/workflows/publish-ats-companies.yml` whenever a
tenant CSV under ``ats-companies/`` lands on ``main``. Behaviour:

1. For each ``ats-companies/<ats>.csv`` upload to
   ``s3://<bucket>/jobhive/v1/<ats>/companies.csv``.
2. Build an aggregated ``companies.{csv,parquet}`` that concatenates
   every per-ATS file with an extra ``ats`` column. Upload immutable,
   content-addressed objects for the manifest.
3. Refresh the stable
   ``s3://<bucket>/jobhive/v1/companies.{csv,parquet}`` aliases for
   backwards compatibility. A failed pair update restores both aliases
   to their previous generation before retrying or failing.
4. Patch ``manifest.json`` in place: refresh the top-level
   ``companies`` entry and the per-ATS ``by_ats_companies`` map. Other
   fields (``by_ats`` for jobs, ``all``, ``stats``…) are preserved
   untouched — they're owned by the publisher pipeline, not the CI.
5. Delete the now-obsolete legacy paths
   (``companies/all.csv`` + ``companies/by-ats/*``).

Notes:
- The script is idempotent. Running it twice in a row produces the
  same R2 state.
- Hashes are computed locally (sha256) so consumers can verify
  downloads without trusting the bucket's ETag.
- Parquet is generated with ``pandas.to_parquet`` (snappy by default).
  Schema: ``ats,name,slug,url``. The ``slug`` column was introduced in
  the 2026-05 migration as the scraper/API identifier; ``url`` holds
  the canonical public careers URL. Per-ATS files that haven't been
  migrated yet (still ``name,url``) get an empty ``slug`` column in
  the aggregate so the published schema stays uniform.

Manifest updates use conditional writes. If the jobs publisher wins the
read-modify-write race, this script reloads the new manifest, reapplies
the companies patch, and retries instead of reverting jobs metadata.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
ATS_COMPANIES_DIR = REPO_ROOT / "ats-companies"
PREFIX = "jobhive/v1"
DISABLED_ATS = frozenset({"seek"})
CACHE_CONTROL_LATEST = "public, max-age=300"
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"
# Lowest manifest version this script knows how to write. Treat as a
# floor: bump existing manifests up to it, never down. If the
# jobs-side publisher independently moves the manifest to a newer
# version, that wins until this constant catches up.
MIN_MANIFEST_VERSION = "2.0"
MANIFEST_WRITE_ATTEMPTS = 5
ALIAS_WRITE_ATTEMPTS = 3


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"FATAL: env var {name} is not set")
    return value


def make_client():
    return boto3.client(
        "s3",
        endpoint_url=env("R2_ENDPOINT_URL"),
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def csv_row_count(data: bytes) -> int:
    """Count rows excluding the header. Cheap because we already have
    the bytes in memory."""
    text = data.decode("utf-8", errors="replace")
    n = text.count("\n")
    if not text.endswith("\n"):
        n += 1
    return max(n - 1, 0)  # subtract header


def read_csv(path: Path) -> bytes:
    return path.read_bytes()


def upload(
    client,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
    *,
    cache_control: str | None = None,
    if_match: str | None = None,
    if_none_match: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ContentType": content_type,
    }
    if cache_control is not None:
        kwargs["CacheControl"] = cache_control
    if if_match is not None:
        kwargs["IfMatch"] = if_match
    if if_none_match is not None:
        kwargs["IfNoneMatch"] = if_none_match
    client.put_object(
        **kwargs,
    )
    print(f"  put s3://{bucket}/{key} ({len(body):,} bytes, {content_type})")


def file_entry(url: str, *, data: bytes, parquet_url: str | None = None) -> dict[str, Any]:
    return {
        "csv": url,
        **({"parquet": parquet_url} if parquet_url else {}),
        "rows": csv_row_count(data),
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def build_aggregated(ats_files: dict[str, bytes]) -> tuple[bytes, bytes, int]:
    """Concatenate per-ATS CSVs adding an ``ats`` column. Returns
    ``(csv_bytes, parquet_bytes, row_count)``.

    Aggregated schema is ``ats,name,slug,url``. The ``slug`` column was
    introduced in the 2026-05 migration as the scraper/API identifier;
    ``url`` is now the canonical public careers URL. CSVs that haven't
    been migrated yet (still ``name,url``) get an empty ``slug`` value
    so the published schema is uniform across all ATSes.
    """
    frames: list[pd.DataFrame] = []
    for ats, raw in sorted(ats_files.items()):
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        # Some legacy phenom rows carry a 5-column schema (url, name,
        # company_code, locale, country). For the aggregate we keep
        # only the universal trio to match the documented schema.
        if not {"name", "url"}.issubset(df.columns):
            print(f"  WARN: {ats} CSV missing name/url columns: {df.columns.tolist()}")
            continue
        if "slug" in df.columns:
            df = df[["name", "slug", "url"]].copy()
        else:
            df = df[["name", "url"]].copy()
            df.insert(1, "slug", "")
        df.insert(0, "ats", ats)
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["ats", "name", "slug", "url"]
    )
    csv_buf = io.BytesIO()
    combined.to_csv(csv_buf, index=False)
    parquet_buf = io.BytesIO()
    combined.to_parquet(parquet_buf, index=False, engine="pyarrow", compression="snappy")
    return csv_buf.getvalue(), parquet_buf.getvalue(), len(combined)


def fetch_existing_manifest(
    client,
    bucket: str,
) -> tuple[dict[str, Any], str | None]:
    """Return the manifest and ETag, or a fresh template when absent."""
    key = f"{PREFIX}/manifest.json"
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            print("  no existing manifest — starting fresh")
            return {}, None
        raise
    etag = obj.get("ETag")
    return (
        json.loads(obj["Body"].read().decode("utf-8")),
        etag if isinstance(etag, str) else None,
    )


def _is_manifest_conflict(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status in {409, 412} or code in {
        "ConditionalRequestConflict",
        "PreconditionFailed",
    }


def patch_manifest(
    client,
    bucket: str,
    *,
    aggregate_entry: dict[str, Any],
    by_ats_entries: dict[str, dict[str, Any]],
    aggregate_rows: int,
) -> None:
    key = f"{PREFIX}/manifest.json"
    for attempt in range(1, MANIFEST_WRITE_ATTEMPTS + 1):
        existing, etag = fetch_existing_manifest(client, bucket)
        manifest = {**existing}
        manifest["companies"] = aggregate_entry
        manifest["by_ats_companies"] = by_ats_entries
        stats = manifest.get("stats")
        if isinstance(stats, dict):
            stats["total_companies"] = aggregate_rows
        manifest["updated_at"] = datetime.now(tz=UTC).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        existing_version = manifest.get("version")
        if existing_version is None or _parse_version(
            existing_version
        ) < _parse_version(MIN_MANIFEST_VERSION):
            manifest["version"] = MIN_MANIFEST_VERSION
        manifest.pop("companies_by_ats", None)
        manifest_bytes = json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        try:
            upload(
                client,
                bucket,
                key,
                manifest_bytes,
                "application/json",
                cache_control=CACHE_CONTROL_LATEST,
                if_match=etag,
                if_none_match="*" if etag is None else None,
            )
        except ClientError as exc:
            if not _is_manifest_conflict(exc):
                raise
            if attempt == MANIFEST_WRITE_ATTEMPTS:
                raise RuntimeError(
                    "manifest changed during every companies patch attempt"
                ) from exc
            print(
                "  manifest changed during companies patch; "
                f"retrying ({attempt}/{MANIFEST_WRITE_ATTEMPTS})"
            )
            continue
        return
    raise AssertionError("unreachable")


def delete_objects_checked(
    client,
    bucket: str,
    objects: list[dict[str, str]],
) -> None:
    response = client.delete_objects(
        Bucket=bucket,
        Delete={"Objects": objects},
    )
    errors = (response or {}).get("Errors", [])
    if errors:
        details = ", ".join(
            f"{error.get('Key', '<unknown>')}: "
            f"{error.get('Code', 'unknown error')}"
            for error in errors
        )
        raise RuntimeError(f"R2 object deletion was incomplete: {details}")


def fetch_object_snapshot(
    client,
    bucket: str,
    key: str,
) -> tuple[bytes, str, str | None] | None:
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise
    return (
        obj["Body"].read(),
        obj.get("ContentType") or "application/octet-stream",
        obj.get("CacheControl"),
    )


def restore_object_snapshot(
    client,
    bucket: str,
    key: str,
    snapshot: tuple[bytes, str, str | None] | None,
) -> None:
    if snapshot is None:
        delete_objects_checked(client, bucket, [{"Key": key}])
        return
    body, content_type, cache_control = snapshot
    upload(
        client,
        bucket,
        key,
        body,
        content_type,
        cache_control=cache_control,
    )


def refresh_stable_aliases(
    client,
    bucket: str,
    *,
    csv_key: str,
    csv_data: bytes,
    parquet_key: str,
    parquet_data: bytes,
) -> None:
    aliases = (
        (csv_key, csv_data, "text/csv"),
        (
            parquet_key,
            parquet_data,
            "application/vnd.apache.parquet",
        ),
    )
    snapshots = {
        key: fetch_object_snapshot(client, bucket, key)
        for key, _data, _content_type in aliases
    }
    for attempt in range(1, ALIAS_WRITE_ATTEMPTS + 1):
        try:
            for key, data, content_type in aliases:
                upload(
                    client,
                    bucket,
                    key,
                    data,
                    content_type,
                    cache_control=CACHE_CONTROL_LATEST,
                )
        except Exception:
            try:
                for key, _data, _content_type in aliases:
                    restore_object_snapshot(
                        client,
                        bucket,
                        key,
                        snapshots[key],
                    )
            except Exception as rollback_exc:
                raise RuntimeError(
                    "stable company aliases failed to publish and rollback"
                ) from rollback_exc
            if attempt == ALIAS_WRITE_ATTEMPTS:
                raise
            print(
                "  stable alias pair failed and was rolled back; "
                f"retrying ({attempt}/{ALIAS_WRITE_ATTEMPTS})"
            )
            continue
        return
    raise AssertionError("unreachable")


def delete_legacy(client, bucket: str) -> None:
    """One-shot cleanup of the old companies layout. Idempotent — if
    the prefix is already empty the loop is a no-op."""
    legacy_prefixes = [
        f"{PREFIX}/companies/by-ats/",
        f"{PREFIX}/companies/all.csv",
        f"{PREFIX}/ats-companies/",  # transient prefix from an earlier draft
    ]
    paginator = client.get_paginator("list_objects_v2")
    for prefix in legacy_prefixes:
        to_delete: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Contents", []) or []:
                to_delete.append({"Key": entry["Key"]})
        if not to_delete:
            print(f"  nothing under {prefix}")
            continue
        print(f"  deleting {len(to_delete)} legacy keys under {prefix}")
        # delete_objects max 1000 keys per request — chunk defensively.
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i : i + 1000]
            delete_objects_checked(client, bucket, chunk)


def _parse_version(value: object) -> tuple[int, ...]:
    """Parse ``"<int>.<int>..."`` into a comparable int-tuple.

    Anything we can't read (non-string, non-numeric segment, missing)
    sorts as ``(0,)`` so the floor wins on garbage input rather than
    silently preserving it.
    """
    if not isinstance(value, str):
        return (0,)
    parts: list[int] = []
    for segment in value.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            return (0,)
    return tuple(parts) if parts else (0,)


def public_url(bucket: str, key: str) -> str:
    """Build the canonical public URL written into the manifest entries.

    GitHub Actions injects unset secrets as the empty string (not as a
    missing env var), so `os.environ.get("R2_PUBLIC_BASE_URL", default)`
    can't catch the unset case via its default — we have to test for
    truthiness explicitly.

    There's no good autoderived fallback for R2: the bucket isn't
    publicly addressable as `https://<bucket>` (R2 requires a custom
    domain or `<id>.r2.dev` mapping), and guessing wrong yields broken
    links in `manifest.json`. So when ``R2_PUBLIC_BASE_URL`` is unset,
    we write the relative R2 object key — matching the behaviour of
    `DatasetPublisher._public_or_key`. Consumers can resolve relatives
    against whatever base they prefer.
    """
    del bucket  # only the key is used in the relative-fallback path
    base = (os.environ.get("R2_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        return key
    return f"{base}/{key}"


def main() -> None:
    bucket = env("R2_BUCKET")
    client = make_client()

    csvs = sorted(
        p
        for p in ATS_COMPANIES_DIR.glob("*.csv")
        if p.is_file() and p.stem not in DISABLED_ATS
    )
    if not csvs:
        sys.exit(f"FATAL: no CSVs found under {ATS_COMPANIES_DIR}")

    ats_files: dict[str, bytes] = {}
    by_ats_entries: dict[str, dict[str, Any]] = {}
    for path in csvs:
        ats = path.stem
        data = read_csv(path)
        ats_files[ats] = data
        key = f"{PREFIX}/{ats}/companies.csv"
        by_ats_entries[ats] = file_entry(public_url(bucket, key), data=data)

    agg_csv, agg_parquet, agg_rows = build_aggregated(ats_files)
    csv_sha256 = sha256_bytes(agg_csv)
    parquet_sha256 = sha256_bytes(agg_parquet)
    immutable_csv_key = f"{PREFIX}/company-aggregates/{csv_sha256}.csv"
    immutable_parquet_key = (
        f"{PREFIX}/company-aggregates/{parquet_sha256}.parquet"
    )
    stable_csv_key = f"{PREFIX}/companies.csv"
    stable_parquet_key = f"{PREFIX}/companies.parquet"

    print(f"== Step 1: upload {len(csvs)} per-ATS companies.csv files")
    for ats, data in ats_files.items():
        key = f"{PREFIX}/{ats}/companies.csv"
        upload(client, bucket, key, data, "text/csv")

    print("\n== Step 2: upload aggregated companies.{csv,parquet}")
    upload(
        client,
        bucket,
        immutable_csv_key,
        agg_csv,
        "text/csv",
        cache_control=CACHE_CONTROL_IMMUTABLE,
    )
    upload(
        client,
        bucket,
        immutable_parquet_key,
        agg_parquet,
        "application/vnd.apache.parquet",
        cache_control=CACHE_CONTROL_IMMUTABLE,
    )
    aggregate_entry = {
        "csv": public_url(bucket, immutable_csv_key),
        "parquet": public_url(bucket, immutable_parquet_key),
        "rows": agg_rows,
        "size_bytes": len(agg_csv),
        "sha256": csv_sha256,
        "parquet_size_bytes": len(agg_parquet),
        "parquet_sha256": parquet_sha256,
    }

    print("\n== Step 3: refresh stable aggregate aliases")
    refresh_stable_aliases(
        client,
        bucket,
        csv_key=stable_csv_key,
        csv_data=agg_csv,
        parquet_key=stable_parquet_key,
        parquet_data=agg_parquet,
    )

    print("\n== Step 4: patch manifest.json")
    patch_manifest(
        client,
        bucket,
        aggregate_entry=aggregate_entry,
        by_ats_entries=by_ats_entries,
        aggregate_rows=agg_rows,
    )

    # Disabled-source objects are deliberately retained as unadvertised
    # orphans. The shared manifest previously had no guaranteed cache lifetime,
    # so deleting a stable object could break an indefinitely cached old
    # manifest. Excluding the source from current aggregates and manifests
    # disables publication without creating dangling links for old clients.
    print("\n== Step 5: cleanup legacy paths")
    delete_legacy(client, bucket)

    print(
        f"\nDone. {len(csvs)} ATSes, {agg_rows:,} aggregated rows, "
        f"manifest patched."
    )


if __name__ == "__main__":
    main()
