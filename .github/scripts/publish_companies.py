"""Publish ats-companies/ to Cloudflare R2 + patch the manifest.

Triggered by `.github/workflows/publish-ats-companies.yml` whenever a
tenant CSV under ``ats-companies/`` lands on ``main``. Behaviour:

1. For each ``ats-companies/<ats>.csv`` upload to
   ``s3://<bucket>/<prefix>/<ats>/companies.csv``.
2. Build an aggregated ``companies.{csv,parquet}`` that concatenates
   every per-ATS file with an extra ``ats`` column. Upload to
   ``s3://<bucket>/<prefix>/companies.{csv,parquet}``.
3. Patch ``<prefix>/manifest.json`` in place: refresh the top-level
   ``companies`` entry and the per-ATS ``by_ats_companies`` map. Other
   fields (``by_ats`` for jobs, ``all``, ``stats``…) are preserved
   untouched — they're owned by the publisher pipeline, not the CI.
   A prefix without a manifest is skipped rather than seeded with a
   partial one.
4. Delete the now-obsolete legacy paths
   (``companies/all.csv`` + ``companies/by-ats/*``).

Every step runs for each prefix in ``PREFIXES``: the canonical
``jobs/v1`` and its deprecated mirror ``jobhive/v1``. Manifest URLs in
both point to ``jobs/v1``.

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

Known limitation — manifest read-modify-write race:
  This script and ``DatasetPublisher`` both read+modify+write
  ``manifest.json``. The workflow's ``concurrency`` group prevents two
  CI runs from overlapping, but a manual publisher run that fires while
  CI is in flight would each read the same manifest version and the
  later writer would clobber the earlier writer's fields. Practical
  risk is low (manual publisher runs are rare and operator-driven), but
  if it ever bites, swap the final ``put_object`` for a conditional
  ``put_object(IfMatch=etag)`` retry loop using the etag from the
  ``get_object`` response.
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

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
ATS_COMPANIES_DIR = REPO_ROOT / "ats-companies"
CANONICAL_PREFIX = "jobs/v1"
# Deprecated prefixes kept as full mirrors of the canonical one.
MIRROR_PREFIXES = ("jobhive/v1",)
PREFIXES = (CANONICAL_PREFIX, *MIRROR_PREFIXES)
# Lowest manifest version this script knows how to write. Treat as a
# floor: bump existing manifests up to it, never down. If the
# jobs-side publisher independently moves the manifest to a newer
# version, that wins until this constant catches up.
MIN_MANIFEST_VERSION = "2.0"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"FATAL: env var {name} is not set")
    return value


def make_client():
    # Lazy import: tests load this module without boto3 installed.
    import boto3

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


def upload(client, bucket: str, key: str, body: bytes, content_type: str) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
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


def fetch_existing_manifest(client, bucket: str, prefix: str) -> dict[str, Any] | None:
    """Return the manifest under ``prefix``, or ``None`` if there is none."""
    key = f"{prefix}/manifest.json"
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


def patch_manifest(
    manifest: dict[str, Any],
    *,
    companies: dict[str, Any],
    by_ats_companies: dict[str, dict[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    """Return a copy of ``manifest`` with the companies block replaced."""
    patched = dict(manifest)
    patched["companies"] = companies
    patched["by_ats_companies"] = by_ats_companies
    patched["updated_at"] = updated_at
    # Version floor: raise to MIN_MANIFEST_VERSION, never lower a newer one.
    if _parse_version(patched.get("version")) < _parse_version(MIN_MANIFEST_VERSION):
        patched["version"] = MIN_MANIFEST_VERSION
    # Legacy companies field whose files `delete_legacy` removes; its name is
    # one underscore away from `by_ats_companies`.
    patched.pop("companies_by_ats", None)
    return patched


def delete_legacy(client, bucket: str, prefix: str) -> None:
    """One-shot cleanup of the old companies layout. Idempotent — if
    the prefix is already empty the loop is a no-op."""
    legacy_prefixes = [
        f"{prefix}/companies/by-ats/",
        f"{prefix}/companies/all.csv",
        f"{prefix}/ats-companies/",  # transient prefix from an earlier draft
        f"{prefix}/seek/companies.csv",  # disabled scheduled source
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
            client.delete_objects(Bucket=bucket, Delete={"Objects": chunk})


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

    csvs = sorted(p for p in ATS_COMPANIES_DIR.glob("*.csv") if p.is_file())
    if not csvs:
        sys.exit(f"FATAL: no CSVs found under {ATS_COMPANIES_DIR}")

    print(f"== Step 1: upload {len(csvs)} per-ATS companies.csv files")
    ats_files: dict[str, bytes] = {}
    by_ats_entries: dict[str, dict[str, Any]] = {}
    for path in csvs:
        ats = path.stem
        data = read_csv(path)
        ats_files[ats] = data
        for prefix in PREFIXES:
            upload(client, bucket, f"{prefix}/{ats}/companies.csv", data, "text/csv")
        by_ats_entries[ats] = file_entry(
            public_url(bucket, f"{CANONICAL_PREFIX}/{ats}/companies.csv"), data=data
        )

    print("\n== Step 2: build + upload aggregated companies.{csv,parquet}")
    agg_csv, agg_parquet, agg_rows = build_aggregated(ats_files)
    for prefix in PREFIXES:
        upload(client, bucket, f"{prefix}/companies.csv", agg_csv, "text/csv")
        upload(
            client,
            bucket,
            f"{prefix}/companies.parquet",
            agg_parquet,
            "application/vnd.apache.parquet",
        )
    aggregate_entry = {
        "csv": public_url(bucket, f"{CANONICAL_PREFIX}/companies.csv"),
        "parquet": public_url(bucket, f"{CANONICAL_PREFIX}/companies.parquet"),
        "rows": agg_rows,
        "size_bytes": len(agg_csv),
        "sha256": sha256_bytes(agg_csv),
        "parquet_size_bytes": len(agg_parquet),
        "parquet_sha256": sha256_bytes(agg_parquet),
    }

    print("\n== Step 3: patch manifests")
    updated_at = datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    patched_count = 0
    for prefix in PREFIXES:
        manifest = fetch_existing_manifest(client, bucket, prefix)
        if manifest is None:
            print(f"  no manifest under {prefix}, skipped")
            continue
        patched = patch_manifest(
            manifest,
            companies=aggregate_entry,
            by_ats_companies=by_ats_entries,
            updated_at=updated_at,
        )
        upload(
            client,
            bucket,
            f"{prefix}/manifest.json",
            json.dumps(patched, indent=2, sort_keys=True).encode("utf-8"),
            "application/json",
        )
        patched_count += 1

    print("\n== Step 4: cleanup legacy paths")
    for prefix in PREFIXES:
        delete_legacy(client, bucket, prefix)

    print(
        f"\nDone. {len(csvs)} ATSes, {agg_rows:,} aggregated rows, "
        f"{patched_count} manifest(s) patched."
    )


if __name__ == "__main__":
    main()
