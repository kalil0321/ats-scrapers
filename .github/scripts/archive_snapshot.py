"""Archive the current public snapshot into a private R2 bucket.

The public dataset is a single snapshot that every publish overwrites, so
yesterday's jobs are gone once today's run lands. This script copies the
current snapshot into a separate, private archive bucket:

    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/manifest.json
    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/all.parquet
    s3://<ARCHIVE_BUCKET>/snapshots/<generated_at>/companies.parquet
    s3://<ARCHIVE_BUCKET>/companies/<last_modified>-<etag>.parquet   # every companies version
    s3://<ARCHIVE_BUCKET>/index.jsonl                         # one line per snapshot

Copies are server-side (no download through the runner). Work that is
already done is skipped, so the workflow can run often and cheaply.

Consistency: both publishers (``pipeline/publisher.py`` and
``.github/scripts/publish_companies.py``) upload their files first and
write ``manifest.json`` last. So:

* a source file newer than the manifest means a publish is in progress:
  skip this run, the next one will pick up the finished publish;
* each source file's ETag is recorded before the copy and checked again
  after it, together with the manifest's ETag; any change aborts the
  snapshot (copied files are deleted) instead of archiving a mix of two
  publishes;
* the archived ``all.parquet`` is hashed and compared with the
  ``parquet_sha256`` the manifest declares.

``manifest.json`` is written last and marks completion. It is kept verbatim
as provenance: its URLs point to the public files of that day, while the
archived copies sit next to it in the same folder.

The archive bucket must NOT have a public domain attached: it holds the
history, which is not part of the free dataset.

Env:
    R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY  (required)
    R2_BUCKET          source bucket (the public dataset)    (required)
    ARCHIVE_BUCKET     destination bucket (private)          (required)
    SOURCE_PREFIX      default "jobhive/v1"
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

MISSING = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
TRANSFER = TransferConfig(multipart_threshold=256 * 1024**2, multipart_chunksize=256 * 1024**2)


class SourceChangedError(Exception):
    """A publish touched the source while we were copying it."""


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


def is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in MISSING


def head(client, bucket: str, key: str) -> dict | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if is_missing(exc):
            return None
        raise


def ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        return
    except ClientError as exc:
        if not is_missing(exc):
            code = exc.response.get("Error", {}).get("Code")
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


def stamp_of(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def copy_checked(client, src_bucket, src_key, src_head, dst_bucket, dst_key, manifest_etag, manifest_key):
    """Server-side copy, then confirm neither the file nor the manifest moved."""
    client.copy(
        {"Bucket": src_bucket, "Key": src_key}, dst_bucket, dst_key, Config=TRANSFER
    )
    after = head(client, src_bucket, src_key)
    manifest_after = head(client, src_bucket, manifest_key)
    if (
        after is None
        or after["ETag"] != src_head["ETag"]
        or manifest_after is None
        or manifest_after["ETag"] != manifest_etag
    ):
        raise SourceChangedError(f"{src_key} or the manifest changed during the copy")
    copied = head(client, dst_bucket, dst_key)
    if copied is None or copied["ContentLength"] != src_head["ContentLength"]:
        raise SourceChangedError(f"size mismatch after copying {src_key}")
    return src_head["ContentLength"]


def sha256_of(client, bucket: str, key: str) -> str:
    h = hashlib.sha256()
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(8 * 1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def read_index(client, bucket: str) -> bytes:
    try:
        return client.get_object(Bucket=bucket, Key="index.jsonl")["Body"].read()
    except ClientError as exc:
        if is_missing(exc):
            return b""
        raise  # never start a fresh index on a transient error: it would erase history


def main() -> None:
    client = make_client()
    src_bucket = env("R2_BUCKET")
    dst_bucket = env("ARCHIVE_BUCKET")
    prefix = env("SOURCE_PREFIX", "jobhive/v1").rstrip("/")

    if src_bucket == dst_bucket:
        sys.exit("FATAL: ARCHIVE_BUCKET must differ from the public R2_BUCKET")

    ensure_bucket(client, dst_bucket)

    manifest_key = f"{prefix}/manifest.json"
    obj = client.get_object(Bucket=src_bucket, Key=manifest_key)
    manifest_bytes = obj["Body"].read()
    manifest_etag, manifest_mtime = obj["ETag"], obj["LastModified"]
    manifest = json.loads(manifest_bytes)

    generated_at = manifest["generated_at"]
    stamp = stamp_of(datetime.fromisoformat(generated_at.replace("Z", "+00:00")))
    folder = f"snapshots/{stamp}"

    sources = {
        "all.parquet": key_from_entry((manifest.get("all") or {}).get("parquet"), prefix),
        "companies.parquet": key_from_entry(
            (manifest.get("companies") or {}).get("parquet"), prefix
        ),
    }
    if not sources["all.parquet"]:
        sys.exit("FATAL: manifest has no all.parquet entry")

    heads = {}
    for name, src_key in sources.items():
        if not src_key:
            print(f"skip {name}: not in manifest")
            continue
        h = head(client, src_bucket, src_key)
        if h is None:
            sys.exit(f"FATAL: {src_key} listed in the manifest but missing")
        if h["LastModified"] > manifest_mtime:
            print(f"{src_key} is newer than the manifest: publish in progress, retry next run")
            return
        heads[name] = h

    summary_lines: list[str] = []

    # 1. The snapshot itself (jobs + companies at that publish).
    if head(client, dst_bucket, f"{folder}/manifest.json") is None:
        copied_keys = []
        try:
            for name, h in heads.items():
                dst_key = f"{folder}/{name}"
                copied_keys.append(dst_key)
                size = copy_checked(
                    client, src_bucket, sources[name], h, dst_bucket, dst_key,
                    manifest_etag, manifest_key,
                )
                print(f"copied {sources[name]} -> {dst_key} ({size:,} bytes)")
            # Strongest check: the manifest records the all.parquet hash, so
            # the archived bytes must be exactly the ones this manifest describes.
            expected = (manifest.get("all") or {}).get("parquet_sha256")
            if expected and "all.parquet" in heads:
                got = sha256_of(client, dst_bucket, f"{folder}/all.parquet")
                if got != expected:
                    raise SourceChangedError("all.parquet does not match the manifest sha256")
                print("all.parquet sha256 matches the manifest")
        except SourceChangedError as exc:
            for key in copied_keys:
                client.delete_object(Bucket=dst_bucket, Key=key)
            # Fail the run (red in Actions, email to the owner) so a mismatch
            # that keeps repeating cannot silently stop the history.
            sys.exit(f"aborted snapshot {stamp}: {exc}. Retrying next run.")
        # Manifest last: its presence marks the snapshot as complete.
        client.put_object(
            Bucket=dst_bucket,
            Key=f"{folder}/manifest.json",
            Body=manifest_bytes,
            ContentType="application/json",
        )
        summary_lines.append(f"archived snapshot {generated_at}")
    else:
        print(f"snapshot {stamp} already archived")

    # 2. Index: add the snapshot if it is missing (also repairs a run that
    #    stopped between the manifest write and the index write).
    index = read_index(client, dst_bucket)
    folders = {
        json.loads(line).get("folder") for line in index.decode().splitlines() if line.strip()
    }
    if folder not in folders:
        sizes = {}
        for name in heads:
            h = head(client, dst_bucket, f"{folder}/{name}")
            if h is not None:
                sizes[name] = h["ContentLength"]
        line = {
            "generated_at": generated_at,
            "folder": folder,
            "archived_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "total_jobs": (manifest.get("stats") or {}).get("total_jobs"),
            "bytes": sizes,
        }
        client.put_object(
            Bucket=dst_bucket,
            Key="index.jsonl",
            Body=index + (json.dumps(line) + "\n").encode(),
            ContentType="application/x-ndjson",
        )
        print("indexed", json.dumps(line))
        summary_lines.append(f"indexed {folder} ({line['total_jobs']} jobs)")

    # 3. Companies change on their own schedule (ats-companies/ pushes keep
    #    the jobs generated_at), so version them separately.
    if "companies.parquet" in heads:
        h = heads["companies.parquet"]
        etag = h["ETag"].strip('"')[:12]
        dst_key = f"companies/{stamp_of(h['LastModified'])}-{etag}.parquet"
        if head(client, dst_bucket, dst_key) is None:
            try:
                size = copy_checked(
                    client, src_bucket, sources["companies.parquet"], h, dst_bucket,
                    dst_key, manifest_etag, manifest_key,
                )
                print(f"copied companies version -> {dst_key} ({size:,} bytes)")
                summary_lines.append(f"archived {dst_key}")
            except SourceChangedError as exc:
                client.delete_object(Bucket=dst_bucket, Key=dst_key)
                print(f"skipped companies version: {exc}. Retrying next run.")

    if not summary_lines:
        print("nothing to do")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and summary_lines:
        with open(summary, "a") as fh:
            fh.write("### Archive\n\n" + "".join(f"- {s}\n" for s in summary_lines))


if __name__ == "__main__":
    main()
