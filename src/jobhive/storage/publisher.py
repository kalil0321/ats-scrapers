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

Memory: every pass is built on polars LazyFrames so no full-corpus
DataFrame is ever materialized.

  Pass 1 — per-ATS lazy: ``pl.scan_csv`` → vectorized enrichment
           expressions → ``sink_csv`` (streaming write to a temp
           file). The same temp CSV is re-scanned to ``sink_parquet``
           (streaming convert) and once more to harvest a thin keys
           frame (small ``collect``). Per-ATS peak is bounded by
           polars' streaming buffers, not the slice's row count.

  Pass 2 — cross-ATS dedup as window functions on the concatenated
           thin keys frame: a single ``sort + filter`` pass per stage
           with ``pl.col(...).first().over(group)`` instead of
           Python-set bookkeeping. The keys frame is the only memory
           peak in this pass.

  Pass 3 — global ``all.parquet`` is built by lazy-scanning each
           per-ATS temp CSV, ``semi``-joining against its survivor
           index frame, ``diagonal_relaxed``-concatenating the parts
           and ``sink_parquet``-streaming the result. Nothing is
           materialized whole.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

from jobhive._version import __version__
from jobhive.enrichment import infer_is_remote, parse_salary_range
from jobhive.exceptions import StorageError
from jobhive.models import ATSType

# Pull the keyword list used by ``infer_is_remote`` so the lazy
# enrichment path can express the rule as vectorized polars
# expressions. The list is optional — if a deploy ships a stripped
# variant of ``derived.py`` that doesn't export it, the publisher
# falls back to the Python callback via ``map_elements``.
try:
    from jobhive.enrichment.derived import REMOTE_KEYWORDS as _REMOTE_KEYWORDS
except ImportError:
    _REMOTE_KEYWORDS = ()

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

# Common pl.scan_csv options across every read path. ``ignore_errors``
# is what lets the scanner fall back to string for a column whose first
# 10k-row sniff says int but later rows hold an alphanumeric ID.
_SCAN_CSV_KWARGS: dict[str, object] = {
    "infer_schema_length": 10000,
    "ignore_errors": True,
}


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

        # ExitStack owns every per-ATS CSV temp: Pass 1 streams each
        # enriched per-ATS slice into one of these, then Pass 3
        # ``scan_csv``s the same files (no re-enrichment) to build
        # the global all.parquet. Files are unlinked at function exit.
        with ExitStack() as stack:
            per_ats_csv_paths: dict[str, Path] = {}
            per_ats_entries: dict[ATSType, dict[str, object]] = {}
            schema_union: list[str] = []
            seen_cols: set[str] = set()

            any_csv_found = False
            for ats in ATSType:
                if ats is ATSType.CUSTOM:
                    continue
                source_path = source_dir / ats_csv_pattern.format(ats=ats.value)
                if not source_path.exists():
                    continue
                # Defense against the publisher firing while a scraper
                # is mid-write (cron + ad-hoc publish race): a 0-byte
                # CSV will blow up ``collect_schema`` with NoDataError.
                # Skip the slice for this run; the next cron publish
                # picks it up once the scraper has finished.
                if source_path.stat().st_size == 0:
                    logger.warning(
                        "%s: source CSV is empty (likely mid-write by a "
                        "concurrent scraper); skipping for this publish.",
                        ats.value,
                    )
                    continue
                any_csv_found = True

                # Build the lazy enriched chain for this ATS slice.
                lf = pl.scan_csv(source_path, **_SCAN_CSV_KWARGS)
                lf = lf.with_columns(pl.lit(ats.value).alias("ats_type"))
                lf = _enrich_lazy(lf)

                try:
                    schema_names = lf.collect_schema().names()
                except pl.exceptions.NoDataError:
                    # Header-only or otherwise empty CSV — same recovery
                    # as the size==0 branch above.
                    logger.warning(
                        "%s: source CSV has no rows; skipping.", ats.value,
                    )
                    continue
                for col in schema_names:
                    if col not in seen_cols:
                        seen_cols.add(col)
                        schema_union.append(col)

                csv_path = stack.enter_context(_temp_file(".csv"))
                # ``sink_csv`` runs the lazy chain through polars'
                # streaming engine — the per-ATS slice is never
                # materialized as one DataFrame in RAM.
                lf.sink_csv(csv_path)
                per_ats_csv_paths[ats.value] = csv_path

                entry, _ = self._upload_per_ats_streaming(
                    csv_path=csv_path,
                    base_key=f"{self._prefix}/{ats.value}/jobs",
                )
                per_ats_entries[ats] = entry
                files_uploaded.extend(_collect_uploaded_keys(entry))

            if not any_csv_found:
                raise StorageError(f"No ATS CSVs found in {source_dir}")

            # ---- Pass 2: cross-ATS dedup directly on the per-ATS temp CSVs ---
            # Build the keys frame as a single lazy scan-and-concat chain
            # rather than collecting per-ATS keys into separate eager
            # DataFrames during Pass 1. This avoids the cumulative
            # ~MB-per-ATS resident growth and lets polars' optimizer
            # decide when to materialize.
            survivors, n_raw, n_kept = _dedup_from_per_ats_csvs(
                per_ats_csv_paths
            )
            logger.info(
                "Cross-ATS dedup: %d → %d rows (%d duplicates removed)",
                n_raw,
                n_kept,
                n_raw - n_kept,
            )

            # ---- Pass 3: stream all.parquet from the per-ATS temp CSVs ------
            all_entry = self._stream_write_all_polars(
                per_ats_csv_paths=per_ats_csv_paths,
                survivors=survivors,
                schema_union=schema_union,
                rows_total=n_kept,
            )
            files_uploaded.extend(_collect_uploaded_keys(all_entry))

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

    def _upload_per_ats_streaming(
        self,
        *,
        csv_path: Path,
        base_key: str,
    ) -> tuple[dict[str, object], int]:
        """Upload a per-ATS slice from a sunk temp CSV.

        Hashes + uploads the CSV, then ``scan_csv`` → ``sink_parquet``
        streams the parquet conversion through polars (no full Arrow
        table in RAM). Returns the manifest entry and the row count.
        """
        entry: dict[str, object] = {}

        csv_key = f"{base_key}.csv"
        csv_sha, csv_size = _file_sha_size(csv_path)
        self._r2.upload(
            csv_path,
            csv_key,
            content_type="text/csv",
            cache_control=CACHE_CONTROL_LATEST,
        )
        entry["csv"] = self._public_or_key(csv_key)
        entry["size_bytes"] = csv_size
        entry["sha256"] = csv_sha

        # Counting rows from the temp CSV is cheap and avoids carrying
        # the row count separately from the lazy chain.
        n_rows = (
            pl.scan_csv(csv_path, **_SCAN_CSV_KWARGS)
            .select(pl.len())
            .collect()
            .item()
        )
        entry["rows"] = n_rows

        if self._write_parquet:
            parquet_key = f"{base_key}.parquet"
            with _temp_file(".parquet") as pq_path:
                pl.scan_csv(csv_path, **_SCAN_CSV_KWARGS).sink_parquet(
                    pq_path, compression="zstd"
                )
                pq_sha, pq_size = _file_sha_size(pq_path)
                self._r2.upload(
                    pq_path,
                    parquet_key,
                    content_type="application/vnd.apache.parquet",
                    cache_control=CACHE_CONTROL_LATEST,
                )
            entry["parquet"] = self._public_or_key(parquet_key)
            entry["parquet_size_bytes"] = pq_size
            entry["parquet_sha256"] = pq_sha

        return entry, n_rows

    def _stream_write_all_polars(
        self,
        *,
        per_ats_csv_paths: dict[str, Path],
        survivors: dict[str, pl.DataFrame],
        schema_union: list[str],
        rows_total: int,
    ) -> dict[str, object]:
        """Stream the global ``all.parquet`` from the per-ATS temp CSVs.

        Two stages, both streaming:

        1. Per-ATS — ``scan_csv`` + ``semi``-join against its
           survivor index frame, sunk to a per-ATS temp parquet via
           ``sink_parquet``. Polars's semi-join on a small RHS is
           hash-probe, so the LHS streams.

        2. Merge — the per-ATS temp parquets are concatenated into the
           global ``all.parquet`` via pyarrow's batch-iteration writer
           with a unified schema (different ATSes can have different
           columns; pyarrow promotes to the union and fills missing
           with null). Peak memory in the merge is one Arrow batch
           (~64 k rows, tens of MB), not the full corpus.
        """
        all_entry: dict[str, object] = {"rows": rows_total}
        if "parquet" not in FORMATS_ALL or not self._write_parquet:
            return all_entry

        with ExitStack() as stage_stack:
            per_ats_parquets: list[Path] = []
            for ats in ATSType:
                if ats is ATSType.CUSTOM:
                    continue
                survivor_frame = survivors.get(ats.value)
                if survivor_frame is None or survivor_frame.is_empty():
                    continue
                csv_path = per_ats_csv_paths.get(ats.value)
                if csv_path is None:
                    continue

                pq_temp = stage_stack.enter_context(_temp_file(".parquet"))
                (
                    pl.scan_csv(csv_path, **_SCAN_CSV_KWARGS)
                    .with_row_index(name="_local_idx")
                    .join(survivor_frame.lazy(), on="_local_idx", how="semi")
                    .drop("_local_idx")
                    .sink_parquet(pq_temp, compression="zstd")
                )
                per_ats_parquets.append(pq_temp)

            pq_key = f"{self._prefix}/all.parquet"
            with _temp_file(".parquet") as all_pq:
                if per_ats_parquets:
                    _merge_parquets_streaming(per_ats_parquets, all_pq)
                else:
                    pl.DataFrame(
                        schema=dict.fromkeys(schema_union, pl.String)
                    ).write_parquet(all_pq, compression="zstd")
                pq_sha, pq_size = _file_sha_size(all_pq)
                self._r2.upload(
                    all_pq,
                    pq_key,
                    content_type="application/vnd.apache.parquet",
                    cache_control=CACHE_CONTROL_LATEST,
                )
            all_entry["parquet"] = self._public_or_key(pq_key)
            all_entry["parquet_size_bytes"] = pq_size
            all_entry["parquet_sha256"] = pq_sha
            all_entry["size_bytes"] = pq_size
            all_entry["sha256"] = pq_sha

        return all_entry

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
        # ``updated_at`` is the "manifest last touched" timestamp; both
        # writers (publisher + CI companies workflow) bump it so a
        # client like the homepage that reads only ``updated_at`` for
        # the freshness badge sees the latest write regardless of which
        # writer ran most recently. Format matches what the CI script
        # writes: UTC ``Z``-suffixed seconds.
        manifest["updated_at"] = generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
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


def _key_col_or_empty(schema_names: list[str], name: str) -> pl.Expr:
    """Return ``pl.col(name)`` cast to String + filled, or an empty
    string literal if the column doesn't exist on this slice."""
    if name in schema_names:
        return (
            pl.col(name)
            .cast(pl.String, strict=False)
            .fill_null("")
            .str.strip_chars()
        )
    return pl.lit("", dtype=pl.String)


def _dedup_from_per_ats_csvs(
    per_ats_csv_paths: dict[str, Path],
) -> tuple[dict[str, pl.DataFrame], int, int]:
    """Build keys, run cross-ATS dedup, and return per-ATS survivors.

    The keys frame is sunk to a temp parquet via ``sink_parquet``
    (polars streaming write — peak memory bounded by one Arrow batch,
    not the corpus) before we run the eager dedup on it. This is the
    key memory win vs an in-memory ``pl.concat([..]).collect()``: the
    per-ATS scans are pulled in one ATS at a time, and the keys
    parquet on disk is small (~80 MB / million rows for the eight
    thin string columns we project).

    Returns ``(survivors_by_ats, n_raw, n_kept)``.
    """
    if not per_ats_csv_paths:
        return {}, 0, 0

    key_lfs: list[pl.LazyFrame] = []
    for ats_value, csv_path in per_ats_csv_paths.items():
        scan = pl.scan_csv(csv_path, **_SCAN_CSV_KWARGS)
        schema_names = scan.collect_schema().names()
        klf = scan.with_row_index(name="_local_idx").select(
            [
                pl.col("_local_idx").cast(pl.Int64),
                pl.lit(ats_value, dtype=pl.String).alias("ats_type"),
                pl.lit(
                    ATS_DEDUP_PRIORITY.get(ats_value, 2), dtype=pl.Int32
                ).alias("_priority"),
                _key_col_or_empty(schema_names, "url").alias("url"),
                _key_col_or_empty(schema_names, "title")
                .str.to_lowercase()
                .alias("title"),
                _key_col_or_empty(schema_names, "company")
                .str.to_lowercase()
                .alias("company"),
                _key_col_or_empty(schema_names, "location")
                .str.to_lowercase()
                .alias("location"),
                _key_col_or_empty(schema_names, "ats_id").alias("ats_id"),
            ]
        )
        key_lfs.append(klf)

    keys_chain = (
        pl.concat(key_lfs, how="vertical_relaxed")
        .with_row_index(name="_orig_idx")
        .with_columns(pl.col("_orig_idx").cast(pl.Int64))
    )

    with _temp_file(".parquet") as keys_pq:
        keys_chain.sink_parquet(keys_pq, compression="zstd")
        keys = pl.read_parquet(keys_pq)

    n_raw = keys.height
    survivors = _decide_dedup_survivors_polars(keys)
    n_kept = sum(s.height for s in survivors.values())
    return survivors, n_raw, n_kept


def _decide_dedup_survivors_polars(
    keys: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    """Run the three-pass cross-ATS dedup as window functions.

    All three passes share one upfront ``sort([_priority, _orig_idx])``
    so each "keep first per group" check reduces to
    ``_orig_idx == _orig_idx.first().over(group_col)`` — no Python
    sets, no ``to_list`` materializations, and no ``filter(is_in(big))``
    rebuilding hash tables across passes.

    Returns a dict mapping ``ats_value`` → polars frame with one
    column ``_local_idx`` (the source-CSV row indices to keep). The
    streaming Pass 3 ``semi``-joins each per-ATS scan against this
    frame.
    """
    if keys.is_empty():
        return {}

    work = keys.sort(["_priority", "_orig_idx"])

    # ---- Pass 1: URL exact-match dedup ------------------------------------
    url_keep = (
        (pl.col("url").str.len_bytes() == 0)
        | (pl.col("_orig_idx") == pl.col("_orig_idx").first().over("url"))
    )
    work = work.filter(url_keep)

    # ---- Pass 2: cross-ATS (company, title, location) dedup ---------------
    work = work.with_columns(
        (
            pl.col("company")
            + pl.lit("|")
            + pl.col("title")
            + pl.lit("|")
            + pl.col("location")
        ).alias("_dedup_key")
    )
    ctl_valid = (
        (pl.col("company").str.len_bytes() > 0)
        & (pl.col("title").str.len_bytes() > 0)
    )
    # Only count distinct ats_types AMONG VALID ROWS in each group —
    # invalid (empty c or t) rows must not push a group into "cross-ATS"
    # status.
    n_ats_in_valid_ctl = (
        pl.when(ctl_valid)
        .then(pl.col("ats_type"))
        .otherwise(None)
        .n_unique()
        .over("_dedup_key")
    )
    is_cross_ctl = ctl_valid & (n_ats_in_valid_ctl > 1)
    ctl_keep = ~is_cross_ctl | (
        pl.col("_orig_idx") == pl.col("_orig_idx").first().over("_dedup_key")
    )
    work = work.filter(ctl_keep).drop("_dedup_key")

    # ---- Pass 3: cross-ATS (company_norm, ats_id) dedup -------------------
    work = work.with_columns(
        pl.col("company")
        .str.replace_all(r"[^a-z0-9]", "")
        .alias("_company_norm")
    )
    work = work.with_columns(
        (pl.col("_company_norm") + pl.lit("|") + pl.col("ats_id")).alias("_cid_key")
    )
    cid_valid = (
        (pl.col("_company_norm").str.len_bytes() > 0)
        & (pl.col("ats_id").str.len_bytes() > 0)
    )
    n_ats_in_valid_cid = (
        pl.when(cid_valid)
        .then(pl.col("ats_type"))
        .otherwise(None)
        .n_unique()
        .over("_cid_key")
    )
    is_cross_cid = cid_valid & (n_ats_in_valid_cid > 1)
    cid_keep = ~is_cross_cid | (
        pl.col("_orig_idx") == pl.col("_orig_idx").first().over("_cid_key")
    )
    work = work.filter(cid_keep).drop("_cid_key", "_company_norm")

    # Materialize per-ATS survivor frames keyed on _local_idx for Pass 3.
    # ``partition_by`` returns a dict of (key,) → frame; we keep only
    # ``_local_idx`` so the survivor frames stay tiny (one int column
    # per surviving row, ~8 MB per million rows).
    survivors: dict[str, pl.DataFrame] = {}
    parts = work.partition_by("ats_type", as_dict=True)
    for key_tuple, part in parts.items():
        ats_value = key_tuple[0] if isinstance(key_tuple, tuple) else key_tuple
        survivors[str(ats_value)] = part.select("_local_idx")
    return survivors


# --- helpers ---------------------------------------------------------------


def _enrich_lazy(lf: pl.LazyFrame) -> pl.LazyFrame:
    """Add ``is_remote`` / ``salary_min`` / ``salary_max`` columns when
    they aren't already present on the input.

    Implemented as polars expressions whenever possible so the lazy
    chain stays streamable through ``sink_csv``. ``is_remote`` reads
    the ``REMOTE_KEYWORDS`` and ``ONSITE_KEYWORDS`` lists from the
    canonical :mod:`jobhive.enrichment.derived` module — both are
    optional, so a deploy that has narrowed the heuristic to title-only
    (no ``ONSITE_KEYWORDS`` exported) still gets a usable column.

    ``salary_summary`` parsing is the only path that has to go through
    a Python callback (``map_elements``); polars' streaming engine
    doesn't run user functions, so the lazy chain falls back to the
    eager engine for that ATS slice (rare — most scrapers populate
    ``salary_min`` / ``salary_max`` upstream and skip this branch).
    """
    schema_names = lf.collect_schema().names()

    if "title" in schema_names and "is_remote" not in schema_names:
        lf = lf.with_columns(_is_remote_expr().alias("is_remote"))

    if "salary_summary" in schema_names and "salary_min" not in schema_names:
        salary_struct = pl.Struct({"min": pl.Float64, "max": pl.Float64})
        lf = (
            lf.with_columns(
                pl.col("salary_summary")
                .map_elements(_safe_parse_salary, return_dtype=salary_struct)
                .alias("_salary_parsed")
            )
            .with_columns(
                pl.col("_salary_parsed").struct.field("min").alias("salary_min"),
                pl.col("_salary_parsed").struct.field("max").alias("salary_max"),
            )
            .drop("_salary_parsed")
        )

    return lf


def _is_remote_expr() -> pl.Expr:
    """Vectorized polars version of :func:`infer_is_remote`.

    Reads ``title`` (not ``location``) — the canonical heuristic
    in :mod:`jobhive.enrichment.derived` is intentionally narrow and
    only treats title-level remote markers as definitive. Free-form
    location text is left for the downstream LLM enrichment pipeline.

    Falls back to the eager ``map_elements`` callback when the deploy
    ships a stripped variant of ``derived.py`` that doesn't export
    ``REMOTE_KEYWORDS`` — the publisher stays usable, but that branch
    breaks lazy streaming for the slice that needs it.
    """
    if not _REMOTE_KEYWORDS:
        return (
            pl.col("title")
            .map_elements(infer_is_remote, return_dtype=pl.Boolean)
        )

    title_lower = (
        pl.col("title").cast(pl.String, strict=False).str.to_lowercase()
    )
    remote_match: pl.Expr = pl.lit(False)
    for kw in _REMOTE_KEYWORDS:
        remote_match = remote_match | title_lower.str.contains(kw, literal=True)
    # Narrow heuristic — never returns False; absence of a remote
    # marker in the title is not evidence the role is on-site.
    return pl.when(remote_match).then(pl.lit(True)).otherwise(None)


def _safe_parse_salary(value: object) -> dict[str, float | None]:
    if not isinstance(value, str):
        return {"min": None, "max": None}
    mn, mx = parse_salary_range(value)
    return {"min": mn, "max": mx}


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


def _merge_parquets_streaming(input_paths: list[Path], out_path: Path) -> None:
    """Merge multiple parquet files into one via polars lazy concat.

    Different ATSes have heterogeneous schemas — same column name,
    different inferred dtype (an int64 ``ats_id`` on one ATS, a
    large_string on another) — and ``pyarrow.unify_schemas`` refuses
    to reconcile those. Polars' ``how="diagonal_relaxed"`` promotes
    conflicting dtypes to the wider one (string wins), then
    ``sink_parquet`` writes the unified result without buffering the
    full corpus.
    """
    if not input_paths:
        return
    lfs = [pl.scan_parquet(str(p)) for p in input_paths]
    pl.concat(lfs, how="diagonal_relaxed").sink_parquet(
        out_path, compression="zstd"
    )


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
