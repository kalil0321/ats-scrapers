"""Archive the current public snapshot into a private R2 bucket.

The public dataset is a single snapshot that every publish overwrites, so
yesterday's jobs are gone once today's run lands. This script copies the
current snapshot (manifest, ``all.parquet``, ``companies.parquet``) into a
separate, private archive bucket, one folder per publish:

    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/manifest.json
    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/all.parquet
    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/companies.parquet
    s3://<ARCHIVE_BUCKET>/index.jsonl          # one line per archived publish

Copies are server-side (no download through the runner). A publish that is
already archived is skipped, so the workflow can run often and cheaply.

The archive bucket must NOT have a public domain attached: it holds the
history, which is not part of the free dataset.

Env:
    R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY  (required)
    R2_BUCKET          source bucket (the public dataset)    (required)
    ARCHIVE_BUCKET     destination bucket (private)          (required)
    SOURCE_PREFIX      default "jobhive/v1"
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
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


def exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            sys.exit(
                f"FATAL: cannot access archive bucket {bucket!r} ({code}). "
                "Create it in Cloudflare R2 (no public domain) and give the "
                "R2 token read/write access to it."
            )
    try:
        client.create_bucket(Bucket=bucket)
        print(f"created bucket {bucket}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        sys.exit(
            f"FATAL: archive bucket {bucket!r} does not exist and this token "
            f"cannot create it ({code}). Create it in the Cloudflare dashboard "
            "(R2 > Create bucket, no public access) and give the token access."
        )


def key_from_entry(value: str | None, prefix: str) -> str | None:
    """Manifest entries are either full public URLs or bucket keys."""
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return urlparse(value).path.lstrip("/")
    return value if value.startswith(prefix) else f"{prefix}/{value.lstrip('/')}"


def main() -> None:
    client = make_client()
    src_bucket = env("R2_BUCKET")
    dst_bucket = env("ARCHIVE_BUCKET")
    prefix = env("SOURCE_PREFIX", "jobhive/v1").rstrip("/")

    if src_bucket == dst_bucket:
        sys.exit("FATAL: ARCHIVE_BUCKET must differ from the public R2_BUCKET")

    ensure_bucket(client, dst_bucket)

    manifest_key = f"{prefix}/manifest.json"
    manifest_bytes = client.get_object(Bucket=src_bucket, Key=manifest_key)["Body"].read()
    manifest = json.loads(manifest_bytes)

    generated_at = manifest["generated_at"]
    stamp = (
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        .astimezone(UTC)
        .strftime("%Y-%m-%dT%H-%M-%SZ")
    )
    folder = f"snapshots/{stamp}"

    if exists(client, dst_bucket, f"{folder}/manifest.json"):
        print(f"snapshot {stamp} already archived, nothing to do")
        return

    copies = {
        "all.parquet": key_from_entry((manifest.get("all") or {}).get("parquet"), prefix),
        "companies.parquet": key_from_entry(
            (manifest.get("companies") or {}).get("parquet"), prefix
        ),
    }
    if not copies["all.parquet"]:
        sys.exit("FATAL: manifest has no all.parquet entry")

    transfer = TransferConfig(multipart_threshold=256 * 1024**2, multipart_chunksize=256 * 1024**2)
    copied = {}
    for name, src_key in copies.items():
        if not src_key:
            print(f"skip {name}: not in manifest")
            continue
        size = client.head_object(Bucket=src_bucket, Key=src_key)["ContentLength"]
        client.copy(
            {"Bucket": src_bucket, "Key": src_key},
            dst_bucket,
            f"{folder}/{name}",
            Config=transfer,
        )
        copied[name] = size
        print(f"copied {src_key} -> s3://{dst_bucket}/{folder}/{name} ({size:,} bytes)")

    # Manifest last: its presence marks the snapshot as complete.
    client.put_object(
        Bucket=dst_bucket,
        Key=f"{folder}/manifest.json",
        Body=manifest_bytes,
        ContentType="application/json",
    )

    line = {
        "generated_at": generated_at,
        "folder": folder,
        "archived_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_jobs": (manifest.get("stats") or {}).get("total_jobs"),
        "bytes": copied,
    }
    try:
        index = client.get_object(Bucket=dst_bucket, Key="index.jsonl")["Body"].read()
    except ClientError:
        index = b""
    client.put_object(
        Bucket=dst_bucket,
        Key="index.jsonl",
        Body=index + (json.dumps(line) + "\n").encode(),
        ContentType="application/x-ndjson",
    )
    print(json.dumps(line, indent=2))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### Archived snapshot {generated_at}\n\n")
            fh.write(f"- jobs: {line['total_jobs']:,}\n" if line["total_jobs"] else "")
            for name, size in copied.items():
                fh.write(f"- {name}: {size / 1024**2:,.0f} MB\n")


if __name__ == "__main__":
    main()
