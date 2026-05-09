"""Publish a directory of per-ATS scraped CSVs to Cloudflare R2.

Layout produced under ``<prefix>`` (default ``jobhive/v1``):

    jobhive/v1/manifest.json
    jobhive/v1/all.parquet           # full snapshot, parquet only
    jobhive/v1/<ats>/jobs.csv        # per-ATS jobs slice
    jobhive/v1/<ats>/jobs.parquet    # idem in parquet

Tenant lists (``<ats>/companies.csv`` and the aggregated
``companies.{csv,parquet}``) are owned by the GitHub Actions workflow
``.github/workflows/publish-ats-companies.yml`` — the publisher only
touches the **jobs** side of the bucket. ``manifest.json`` is read,
patched (jobs entries updated, ``companies`` / ``by_ats_companies``
preserved), and re-uploaded so the two writers never clobber each
other.

Old layout (``jobs/all.parquet``, ``jobs/by-ats/*``, ``jobs/by-date/*``,
``companies/*``) is wiped on first run by :meth:`prune_legacy_paths`.

Memory: the publisher never holds more than one per-ATS slice in RAM
at a time. The cross-ATS view (``all.parquet``) is built in three
streaming passes — per-ATS keys are harvested into a thin
(``url, title, company, location, ats_id, ats_type``) frame, dedup
decisions are made on that thin frame, then the per-ATS CSVs are
re-read in pandas chunks (filtered by the survivor set) into a
streamed temp CSV, which pyarrow then converts to parquet
block-by-block. Peak RSS scales with the largest single per-ATS
slice, not the corpus total.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from jobhive._version import __version__
from jobhive.enrichment import infer_is_remote, parse_salary_range
from jobhive.exceptions import StorageError
from jobhive.models import ATSType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jobhive.storage.r2 import R2Client

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "jobhive/v1"
CACHE_CONTROL_LATEST = "public, max-age=300"  # manifest + latest data files

# `all` is parquet-only — the CSV equivalent would be ~150 MB and there's
# no consumer for it that wouldn't prefer parquet.
FORMATS_ALL = ("parquet",)
FORMATS_PER_ATS = ("csv", "parquet")

# Pandas read_csv chunksize for the streaming "all" build. 100k rows
# of typical job data is ~30 MB resident — small enough that several
# chunks coexisting (read + filter + write) stay well under 1 GB.
ALL_STREAM_CHUNKSIZE = 100_000

# Pyarrow CSV → parquet block size for the streaming all.parquet
# write. 64 MiB is large enough to amortize per-batch overhead and
# small enough that peak memory during conversion is bounded.
ALL_PARQUET_BLOCK_BYTES = 64 * 1024 * 1024


@dataclass
class PublishResult:
    """Summary of what was uploaded in a single publish run."""

    manifest_key: str
    files: list[str] = field(default_factory=list)
    total_jobs: int = 0
    total_jobs_raw: int = 0
    ats_count: int = 0
    duration_seconds: float = 0.0


class DatasetPublisher:
    """Builds and publishes a versioned dataset to R2.

    The publisher is responsible for **jobs only**. Companies / tenant
    lists are written by the CI workflow. Both writers share
    ``manifest.json`` via read-modify-write, so the publisher must
    never touch the ``companies`` or ``by_ats_companies`` keys.
    """

    def __init__(
        self,
        r2_client: R2Client,
        *,
        prefix: str = DEFAULT_PREFIX,
        write_parquet: bool = True,
    ) -> None:
        self._r2 = r2_client
        self._prefix = prefix.strip("/")
        self._write_parquet = write_parquet
        if write_parquet:
            try:
                import pyarrow  # noqa: F401
            except ImportError as exc:
                raise StorageError(
                    "pyarrow is required when write_parquet=True. "
                    "Install with `pip install jobhive[publish]`."
                ) from exc

    def publish_from_directory(
        self,
        source_dir: Path,
        *,
        ats_csv_pattern: str = "{ats}/jobs.csv",
    ) -> PublishResult:
        """Publish jobs from a local directory.

        Reads ``<source_dir>/<ats>/jobs.csv`` for every supported ATS,
        produces:

        1. Per-ATS slice ``<prefix>/<ats>/jobs.{csv,parquet}`` (raw —
           no cross-ATS dedup, so single-ATS consumers see what that
           ATS exposes).
        2. Cross-ATS deduped global snapshot ``<prefix>/all.parquet``.
        3. Patched ``<prefix>/manifest.json`` with refreshed
           ``all`` and ``by_ats`` jobs entries; ``companies`` and
           ``by_ats_companies`` (CI-owned) are preserved untouched.

        Then deletes the legacy paths
        (``<prefix>/jobs/*``, ``<prefix>/companies/*``).
        """
        started = datetime.now(tz=UTC)
        files_uploaded: list[str] = []

        # ---- Pass 1: per-ATS upload + thin keys harvest ---------------------
        # Each ATS slice is read, enriched, uploaded, and then released
        # before the next one is loaded. The only thing carried forward
        # is the thin keys frame for cross-ATS dedup.
        per_ats_entries: dict[ATSType, dict[str, object]] = {}
        keys_frames: list[pd.DataFrame] = []
        schema_union: list[str] = []
        seen_cols: set[str] = set()
        cumulative_offset = 0
        ats_lengths: dict[str, int] = {}

        any_csv_found = False
        for ats in ATSType:
            if ats is ATSType.CUSTOM:
                continue
            path = source_dir / ats_csv_pattern.format(ats=ats.value)
            if not path.exists():
                continue
            any_csv_found = True

            df = _read_csv_safely(path)
            df["ats_type"] = ats.value
            _enrich_with_derived(df)

            for col in df.columns:
                if col not in seen_cols:
                    seen_cols.add(col)
                    schema_union.append(col)

            entry = self._upload_dataframe(
                df,
                base_key=f"{self._prefix}/{ats.value}/jobs",
                formats=FORMATS_PER_ATS,
            )
            per_ats_entries[ats] = entry
            files_uploaded.extend(_collect_uploaded_keys(entry))

            keys_frames.append(_extract_dedup_keys(df, ats.value, cumulative_offset))
            ats_lengths[ats.value] = len(df)
            cumulative_offset += len(df)

            # Hand the slice back to the GC before the next ATS loads.
            del df

        if not any_csv_found:
            raise StorageError(f"No ATS CSVs found in {source_dir}")

        # ---- Pass 2: cross-ATS dedup decision on the thin keys frame -------
        keys = pd.concat(keys_frames, ignore_index=True)
        keys_frames.clear()
        n_raw = len(keys)
        survivors_by_ats = _decide_dedup_survivors(keys)
        n_kept = sum(len(s) for s in survivors_by_ats.values())
        del keys
        logger.info(
            "Cross-ATS dedup: %d → %d rows (%d duplicates removed)",
            n_raw,
            n_kept,
            n_raw - n_kept,
        )

        # ---- Pass 3: stream-build all.{csv,parquet} ------------------------
        all_entry = self._stream_write_all(
            source_dir=source_dir,
            ats_csv_pattern=ats_csv_pattern,
            survivors_by_ats=survivors_by_ats,
            schema_union=schema_union,
        )
        files_uploaded.extend(_collect_uploaded_keys(all_entry))

        # ``total_companies`` is sourced from the CI-owned
        # ``by_ats_companies`` block in the existing manifest (if any).
        manifest_key = self._patch_and_upload_manifest(
            generated_at=started,
            stats_factory=lambda existing: {
                "total_jobs": n_kept,
                "total_jobs_raw": n_raw,
                "total_companies": _sum_by_ats_companies_rows(existing),
                "ats_count": len(per_ats_entries),
                "schema_version": "2.0",
                "schema_columns": schema_union,
            },
            all_entry=all_entry,
            by_ats=per_ats_entries,
        )
        files_uploaded.append(manifest_key)

        deleted = self.prune_legacy_paths()
        if deleted:
            logger.info("Deleted %d legacy keys", deleted)

        ended = datetime.now(tz=UTC)
        return PublishResult(
            manifest_key=manifest_key,
            files=files_uploaded,
            total_jobs=n_kept,
            total_jobs_raw=n_raw,
            ats_count=len(per_ats_entries),
            duration_seconds=(ended - started).total_seconds(),
        )

    def prune_legacy_paths(self) -> int:
        """Delete every key under the pre-2.0 layout. Idempotent.

        Companies legacy paths are also deleted by the CI workflow's
        publisher script — calling them here as well makes the
        publisher correct in isolation when the CI hasn't run yet.
        """
        legacy_prefixes = [
            f"{self._prefix}/jobs/",
            f"{self._prefix}/companies/",
        ]
        keys: list[str] = []
        for prefix in legacy_prefixes:
            for obj in self._r2.list(prefix=prefix):
                key = obj.get("Key")
                if key:
                    keys.append(key)
        if not keys:
            return 0
        return self._r2.delete_many(keys)

    # --- internals ---------------------------------------------------------

    def _upload_dataframe(
        self,
        df: pd.DataFrame,
        *,
        base_key: str,
        formats: tuple[str, ...],
    ) -> dict[str, object]:
        entry: dict[str, object] = {"rows": len(df)}

        if "csv" in formats:
            csv_key = f"{base_key}.csv"
            with _temp_file(".csv") as csv_path:
                df.to_csv(csv_path, index=False)
                sha, size = _file_sha_size(csv_path)
                self._r2.upload(
                    csv_path,
                    csv_key,
                    content_type="text/csv",
                    cache_control=CACHE_CONTROL_LATEST,
                )
            entry["csv"] = self._public_or_key(csv_key)
            entry["size_bytes"] = size
            entry["sha256"] = sha

        if "parquet" in formats and self._write_parquet:
            parquet_key = f"{base_key}.parquet"
            with _temp_file(".parquet") as pq_path:
                _normalize_for_parquet_inplace(df).to_parquet(
                    pq_path, index=False, compression="zstd"
                )
                sha, size = _file_sha_size(pq_path)
                self._r2.upload(
                    pq_path,
                    parquet_key,
                    content_type="application/vnd.apache.parquet",
                    cache_control=CACHE_CONTROL_LATEST,
                )
            entry["parquet"] = self._public_or_key(parquet_key)
            entry["parquet_size_bytes"] = size
            entry["parquet_sha256"] = sha
            if "size_bytes" not in entry:
                # Parquet-only artifact (the global ``all``): mirror
                # size + sha into the canonical fields so consumers
                # don't need format-specific lookups.
                entry["size_bytes"] = size
                entry["sha256"] = sha

        return entry

    def _stream_write_all(
        self,
        *,
        source_dir: Path,
        ats_csv_pattern: str,
        survivors_by_ats: dict[str, set[int]],
        schema_union: list[str],
    ) -> dict[str, object]:
        """Stream-build ``all.{csv,parquet}`` from per-ATS source CSVs.

        Pass 3a: chunk-read each per-ATS CSV with pandas; for each
        chunk, drop the rows whose ATS-local index isn't in the
        survivor set, then append to a single growing CSV temp file.

        Pass 3b: hand the temp CSV to pyarrow's streaming CSV reader
        and pipe each ``RecordBatch`` into a ``ParquetWriter`` — the
        compressed parquet bytes are written to disk in chunks rather
        than buffered whole in RAM.

        Per-chunk peak memory is dominated by the pandas chunk
        (``ALL_STREAM_CHUNKSIZE`` rows ≈ 30 MB) plus the pyarrow batch
        (``ALL_PARQUET_BLOCK_BYTES`` ≈ 64 MB). Total peak < 200 MB.
        """
        all_entry: dict[str, object] = {}

        # Always materialize the full intermediate CSV; we use it both
        # to compute size/sha for the FORMATS_ALL == ("csv",) case and
        # as the input to the parquet streaming write.
        with _temp_file(".csv") as csv_path:
            rows_total = self._stream_all_csv(
                source_dir=source_dir,
                ats_csv_pattern=ats_csv_pattern,
                survivors_by_ats=survivors_by_ats,
                schema_union=schema_union,
                out_path=csv_path,
            )
            all_entry["rows"] = rows_total

            if "csv" in FORMATS_ALL:
                csv_key = f"{self._prefix}/all.csv"
                csv_sha, csv_size = _file_sha_size(csv_path)
                self._r2.upload(
                    csv_path,
                    csv_key,
                    content_type="text/csv",
                    cache_control=CACHE_CONTROL_LATEST,
                )
                all_entry["csv"] = self._public_or_key(csv_key)
                all_entry["size_bytes"] = csv_size
                all_entry["sha256"] = csv_sha

            if "parquet" in FORMATS_ALL and self._write_parquet:
                pq_key = f"{self._prefix}/all.parquet"
                with _temp_file(".parquet") as pq_path:
                    self._stream_csv_to_parquet(csv_path, pq_path)
                    pq_sha, pq_size = _file_sha_size(pq_path)
                    self._r2.upload(
                        pq_path,
                        pq_key,
                        content_type="application/vnd.apache.parquet",
                        cache_control=CACHE_CONTROL_LATEST,
                    )
                all_entry["parquet"] = self._public_or_key(pq_key)
                all_entry["parquet_size_bytes"] = pq_size
                all_entry["parquet_sha256"] = pq_sha
                if "size_bytes" not in all_entry:
                    all_entry["size_bytes"] = pq_size
                    all_entry["sha256"] = pq_sha

        return all_entry

    def _stream_all_csv(
        self,
        *,
        source_dir: Path,
        ats_csv_pattern: str,
        survivors_by_ats: dict[str, set[int]],
        schema_union: list[str],
        out_path: Path,
    ) -> int:
        """Append surviving rows from each per-ATS CSV to ``out_path``.

        Returns the total row count written (across all ATSes).
        """
        rows_total = 0
        first_chunk = True

        for ats in ATSType:
            if ats is ATSType.CUSTOM:
                continue
            survivors = survivors_by_ats.get(ats.value)
            if not survivors:
                continue
            path = source_dir / ats_csv_pattern.format(ats=ats.value)
            if not path.exists():
                continue

            local_offset = 0
            for chunk in pd.read_csv(path, chunksize=ALL_STREAM_CHUNKSIZE):
                chunk_size = len(chunk)
                # Survivor membership test against this chunk's local
                # row indices [local_offset, local_offset + chunk_size).
                keep_positions = [
                    pos
                    for pos, local_idx in enumerate(
                        range(local_offset, local_offset + chunk_size)
                    )
                    if local_idx in survivors
                ]
                local_offset += chunk_size
                if not keep_positions:
                    continue

                kept = chunk.iloc[keep_positions]
                kept = kept.assign(ats_type=ats.value)
                _enrich_with_derived(kept)
                # Reindex to the union schema so missing-in-this-ATS
                # columns appear as NaN rather than dropping out.
                kept = kept.reindex(columns=schema_union)
                kept.to_csv(
                    out_path,
                    mode="a" if not first_chunk else "w",
                    header=first_chunk,
                    index=False,
                )
                first_chunk = False
                rows_total += len(kept)

        # Edge case: every per-ATS slice was emptied by dedup. Write a
        # header-only CSV so downstream readers see a parseable file.
        if first_chunk:
            pd.DataFrame(columns=schema_union).to_csv(out_path, index=False)

        return rows_total

    def _stream_csv_to_parquet(self, csv_path: Path, pq_path: Path) -> None:
        """Convert ``csv_path`` to ``pq_path`` block-by-block with pyarrow.

        Pyarrow infers the column types from a header sniff and reads
        the body in fixed-size blocks. Each block is written through a
        single ``ParquetWriter`` so peak memory stays bounded.
        """
        import pyarrow.csv as pacsv
        import pyarrow.parquet as papq

        read_options = pacsv.ReadOptions(block_size=ALL_PARQUET_BLOCK_BYTES)
        reader = pacsv.open_csv(csv_path, read_options=read_options)
        try:
            schema = reader.schema
            writer = papq.ParquetWriter(pq_path, schema, compression="zstd")
            try:
                while True:
                    try:
                        batch = reader.read_next_batch()
                    except StopIteration:
                        break
                    writer.write_batch(batch)
            finally:
                writer.close()
        finally:
            reader.close()

    def _patch_and_upload_manifest(
        self,
        *,
        generated_at: datetime,
        stats_factory,
        all_entry: dict[str, object],
        by_ats: dict[ATSType, dict[str, object]],
    ) -> str:
        """Read existing manifest, replace jobs-related fields, preserve
        the companies block written by the CI."""
        key = f"{self._prefix}/manifest.json"
        existing = _load_existing_manifest(self._r2, key)

        manifest: dict[str, object] = {**existing}
        manifest["version"] = "2.0"
        manifest["generator"] = f"jobhive/{__version__}"
        manifest["generated_at"] = generated_at.isoformat()
        manifest["stats"] = stats_factory(existing)
        manifest["all"] = all_entry
        manifest["by_ats"] = {ats.value: entry for ats, entry in by_ats.items()}

        # Drop fields from the pre-2.0 layout if they survived the
        # legacy-path prune. Their data is gone so the entries point
        # nowhere.
        for legacy in ("by_date", "companies_by_ats"):
            manifest.pop(legacy, None)

        body = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode(
            "utf-8"
        )
        self._r2.upload_bytes(
            body,
            key,
            content_type="application/json",
            cache_control=CACHE_CONTROL_LATEST,
        )
        return key

    def _public_or_key(self, key: str) -> str:
        return self._r2.public_url(key) or key


# --- Cross-ATS dedup -------------------------------------------------------


# When the same (company, title, location) shows up under multiple ATSes,
# we keep the row from the highest-priority ATS (lowest number wins).
ATS_DEDUP_PRIORITY: dict[str, int] = {
    # Direct employer ATSes
    "ashby": 1, "avature": 1, "bamboohr": 1, "breezy": 1, "cornerstone": 1,
    "greenhouse": 1, "icims": 1, "jazzhr": 1, "join_com": 1, "lever": 1,
    "oracle": 1, "personio": 1, "phenom": 1, "pinpoint": 1, "recruitee": 1,
    "recruiterbox": 1, "rippling": 1, "smartrecruiters": 1,
    "successfactors": 1, "taleo": 1, "teamtailor": 1, "workable": 1,
    "workday": 1,
    # Big-tech bespoke careers — also priority 1 (single-tenant, canonical)
    "amazon": 1, "apple": 1, "google": 1, "meta": 1, "tesla": 1,
    "tiktok": 1, "uber": 1,
    # Hybrid jobboards
    "welcometothejungle": 3, "mercor": 3, "gem": 3, "jobvite": 3,
    # Sourcing/matching layer that mirrors others
    "eightfold": 5,
    # National public-sector aggregators — government-curated but the
    # same role often appears here AND on the employer's direct ATS.
    "bundesagentur": 6,
    "arbetsformedlingen": 6,
    "usajobs": 6,
}


def _extract_dedup_keys(
    df: pd.DataFrame, ats_value: str, base_offset: int
) -> pd.DataFrame:
    """Build the thin keys frame for one ATS slice.

    Holds only the columns needed for the three-pass cross-ATS dedup,
    plus ``_orig_idx`` (global concat-order position; preserved across
    streaming so dedup is deterministic vs the pre-streaming impl) and
    ``_local_idx`` (row index within this ATS, used by Pass 3 to
    filter the source CSV when re-reading).
    """
    n = len(df)
    return pd.DataFrame(
        {
            "_orig_idx": range(base_offset, base_offset + n),
            "_local_idx": range(n),
            "_priority": ATS_DEDUP_PRIORITY.get(ats_value, 2),
            "ats_type": ats_value,
            "url": (
                df["url"].fillna("").astype(str).str.strip().reset_index(drop=True)
                if "url" in df.columns
                else pd.Series([""] * n)
            ),
            "title": (
                df["title"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
                .reset_index(drop=True)
                if "title" in df.columns
                else pd.Series([""] * n)
            ),
            "company": (
                df["company"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
                .reset_index(drop=True)
                if "company" in df.columns
                else pd.Series([""] * n)
            ),
            "location": (
                df["location"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
                .reset_index(drop=True)
                if "location" in df.columns
                else pd.Series([""] * n)
            ),
            "ats_id": (
                df["ats_id"].fillna("").astype(str).str.strip().reset_index(drop=True)
                if "ats_id" in df.columns
                else pd.Series([""] * n)
            ),
        }
    )


def _decide_dedup_survivors(keys: pd.DataFrame) -> dict[str, set[int]]:
    """Run the three-pass cross-ATS dedup on a thin keys frame.

    Returns a per-ATS set of surviving ``_local_idx`` values that the
    caller uses to filter the source CSVs when streaming the global
    snapshot. Three passes:

    1. URL exact-match dedup.
    2. Cross-ATS ``(company, title, location)`` dedup.
    3. Cross-ATS ``(company_norm, ats_id)`` dedup.

    Pass 1 always wins over passes 2 and 3 — once a row is dropped, it
    cannot reappear.
    """
    if keys.empty or "ats_type" not in keys.columns:
        return {}

    drop_orig: set[int] = set()
    work = keys

    # ---- Pass 1: URL exact-match dedup ------------------------------------
    has_url = work["url"].str.len() > 0
    if has_url.any():
        url_rows = work.loc[has_url]
        url_kept = (
            url_rows.sort_values(["_priority", "_orig_idx"], kind="stable")
            .drop_duplicates(subset=["url"], keep="first")["_orig_idx"]
        )
        drop_orig.update(set(url_rows["_orig_idx"]) - set(url_kept))
        work = work[~work["_orig_idx"].isin(drop_orig)]

    # ---- Pass 2: cross-ATS (company, title, location) dedup ---------------
    work = work.assign(
        _dedup_key=work["company"] + "|" + work["title"] + "|" + work["location"]
    )
    valid_mask = (work["company"].str.len() > 0) & (work["title"].str.len() > 0)
    valid = work.loc[valid_mask]
    if not valid.empty:
        ats_per_key = valid.groupby("_dedup_key")["ats_type"].transform("nunique")
        cross = valid.loc[ats_per_key > 1]
        if not cross.empty:
            cross_kept = (
                cross.sort_values(["_priority", "_orig_idx"], kind="stable")
                .drop_duplicates(subset=["_dedup_key"], keep="first")["_orig_idx"]
            )
            ctl_drop = set(cross["_orig_idx"]) - set(cross_kept)
            drop_orig.update(ctl_drop)
            work = work[~work["_orig_idx"].isin(ctl_drop)]
    work = work.drop(columns=["_dedup_key"], errors="ignore")

    # ---- Pass 3: cross-ATS (company_norm, ats_id) dedup -------------------
    company_norm = work["company"].str.replace(r"[^a-z0-9]", "", regex=True)
    work = work.assign(_cid_key=company_norm + "|" + work["ats_id"])
    cid_valid_mask = (company_norm.str.len() > 0) & (work["ats_id"].str.len() > 0)
    cid_valid = work.loc[cid_valid_mask]
    if not cid_valid.empty:
        ats_count = cid_valid.groupby("_cid_key")["ats_type"].transform("nunique")
        cross_cid = cid_valid.loc[ats_count > 1]
        if not cross_cid.empty:
            cid_kept = (
                cross_cid.sort_values(["_priority", "_orig_idx"], kind="stable")
                .drop_duplicates(subset=["_cid_key"], keep="first")["_orig_idx"]
            )
            drop_orig.update(set(cross_cid["_orig_idx"]) - set(cid_kept))

    # Materialize per-ATS survivor sets keyed on (_local_idx within ATS)
    # so Pass 3 of the publish run can filter source CSVs by row number.
    survivors: dict[str, set[int]] = {}
    survivors_mask = ~keys["_orig_idx"].isin(drop_orig)
    surviving_rows = keys.loc[survivors_mask, ["ats_type", "_local_idx"]]
    for ats_value, group in surviving_rows.groupby("ats_type"):
        survivors[str(ats_value)] = set(group["_local_idx"].astype(int).tolist())
    return survivors


# --- helpers ---------------------------------------------------------------


def _enrich_with_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``is_remote`` and ``salary_min``/``salary_max`` derived columns
    when the source data lacks them.

    Mutates ``df`` in-place (returns it for chaining). At publisher
    scale a defensive ``.copy()`` of a 3M+ row frame is the difference
    between fitting in RAM and OOM-kill — the caller owns ``df`` and
    no other consumer references it.
    """
    if "location" in df.columns and "is_remote" not in df.columns:
        df["is_remote"] = df["location"].apply(infer_is_remote)
    if "salary_summary" in df.columns and "salary_min" not in df.columns:
        parsed = df["salary_summary"].apply(parse_salary_range)
        df["salary_min"] = parsed.apply(lambda t: t[0])
        df["salary_max"] = parsed.apply(lambda t: t[1])
    return df


def _normalize_for_parquet_inplace(df: pd.DataFrame) -> pd.DataFrame:
    """Cast object-dtype columns to typed nullable for parquet stability.

    Mutates ``df`` in-place — at corpus scale the previous defensive
    copy doubled peak memory of the frame. Caller is the publisher and
    does not reuse ``df`` after this call.
    """
    for col in df.columns:
        if df[col].dtype != object:
            continue
        non_null = df[col].dropna()
        if not non_null.empty and non_null.map(lambda v: isinstance(v, bool)).all():
            df[col] = df[col].astype("boolean")
        else:
            df[col] = df[col].astype("string")
    return df


def _read_csv_safely(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, low_memory=False)
    except Exception as exc:
        raise StorageError(f"Failed to read CSV {path}: {exc}") from exc


def _collect_uploaded_keys(entry: dict[str, object]) -> list[str]:
    keys: list[str] = []
    for field_name in ("csv", "parquet"):
        value = entry.get(field_name)
        if isinstance(value, str):
            keys.append(value)
    return keys


def _sum_by_ats_companies_rows(manifest: dict[str, object]) -> int:
    """Sum ``rows`` across every ``by_ats_companies.<ats>`` entry.

    Companies are CI-owned, so the publisher derives ``total_companies``
    from whatever the CI most recently wrote — fallback to 0 when the
    CI hasn't run yet."""
    block = manifest.get("by_ats_companies")
    if not isinstance(block, dict):
        return 0
    total = 0
    for entry in block.values():
        if isinstance(entry, dict):
            rows = entry.get("rows")
            if isinstance(rows, int):
                total += rows
    return total


def _load_existing_manifest(r2_client: R2Client, key: str) -> dict[str, object]:
    """Best-effort fetch of an existing manifest. On any failure (missing
    object, malformed JSON) return an empty dict so the publisher
    proceeds with a fresh manifest rather than crashing the run."""
    try:
        body = r2_client.get_bytes(key)
    except StorageError as exc:
        logger.warning("Could not read existing manifest %s: %s", key, exc)
        return {}
    if not body:
        return {}
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "Existing manifest %s did not parse as JSON (%s); starting fresh",
            key,
            exc,
        )
        return {}
    if not isinstance(loaded, dict):
        logger.warning(
            "Existing manifest %s root is not an object; starting fresh", key
        )
        return {}
    return loaded


@contextmanager
def _temp_file(suffix: str) -> Iterator[Path]:
    """Context manager yielding a temp file path that is unlinked on exit."""
    fd, path_str = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(path_str)
    try:
        yield path
    finally:
        with suppress(FileNotFoundError):
            path.unlink()


def _file_sha_size(path: Path) -> tuple[str, int]:
    """Stream-hash a file and return ``(sha256_hex, size_bytes)``."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
