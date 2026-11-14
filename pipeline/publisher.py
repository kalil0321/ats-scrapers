"""Publish a directory of per-ATS scraped CSVs to Cloudflare R2.

Layout produced under ``<prefix>`` (default ``jobhive/v1``):

    jobhive/v1/manifest.json
    jobhive/v1/badges.json           # pre-formatted README badge values
    jobhive/v1/all.{csv,parquet}     # full snapshot, both formats
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
import re
import tempfile
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from ats_scrapers._version import __version__
from ats_scrapers.enrichment import infer_is_remote, parse_salary_range
from ats_scrapers.exceptions import StorageError
from ats_scrapers.models import ATSType
from ats_scrapers.source_kind import HEADLINE_KINDS, source_kind
from pipeline.crossdedup import title_pass_drops

# Pull the keyword list used by ``infer_is_remote`` so the lazy
# enrichment path can express the rule as vectorized polars
# expressions. The list is optional — if a deploy ships a stripped
# variant of ``derived.py`` that doesn't export it, the publisher
# falls back to the Python callback via ``map_elements``.
try:
    from ats_scrapers.enrichment.derived import REMOTE_KEYWORDS as _REMOTE_KEYWORDS
except ImportError:
    _REMOTE_KEYWORDS = ()

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pipeline.r2 import R2Client

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "jobhive/v1"
# Written by scripts/run_pipeline.py next to each provider's jobs.csv.
TENANT_STATUS_FILENAME = "tenant_status.csv"
CACHE_CONTROL_LATEST = "public, max-age=300"  # manifest + latest data files

# ``all`` ships both formats — parquet for typed pandas / DuckDB
# consumers, CSV (~2.3 GB at the current ~4M-row corpus) for
# spreadsheet, ``grep``, and tools that don't speak parquet. The CSV
# is built by streaming the merged parquet back through polars, so
# the per-row data lives on disk in two places but never both in
# RAM.
FORMATS_ALL = ("csv", "parquet")
FORMATS_PER_ATS = ("csv", "parquet")

# Common pl.scan_csv options across every read path. ``ignore_errors``
# is what lets the scanner fall back to string for a column whose first
# 10k-row sniff says int but later rows hold an alphanumeric ID.
_SCAN_CSV_KWARGS: dict[str, object] = {
    "infer_schema_length": 10000,
    "ignore_errors": True,
}


# Sources the scheduled pipeline no longer runs. A jobs file an earlier run
# left on the operator's disk is ignored instead of being republished.
RETIRED_SOURCES: frozenset[str] = frozenset({"seek"})


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
        write_all_csv: bool = True,
    ) -> None:
        self._r2 = r2_client
        self._prefix = prefix.strip("/")
        self._write_parquet = write_parquet
        self._write_all_csv = write_all_csv
        if write_parquet:
            try:
                import pyarrow  # noqa: F401
            except ImportError as exc:
                raise StorageError(
                    "pyarrow is required when write_parquet=True. "
                    "Install with `pip install ats-scrapers[publish]`."
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
        2. Cross-ATS deduped global snapshot ``<prefix>/all.{csv,parquet}``.
        3. Patched ``<prefix>/manifest.json`` with refreshed
           ``all`` and ``by_ats`` jobs entries; ``companies`` and
           ``by_ats_companies`` (CI-owned) are preserved untouched.

        Then deletes the legacy paths
        (``<prefix>/jobs/*``, ``<prefix>/companies/*``).
        """
        started = datetime.now(tz=UTC)
        files_uploaded: list[str] = []
        manifest_key = f"{self._prefix}/manifest.json"
        existing_manifest = _load_existing_manifest(self._r2, manifest_key)
        _guard_suspicious_empty_job_slices(
            source_dir=source_dir,
            ats_csv_pattern=ats_csv_pattern,
            existing_manifest=existing_manifest,
        )

        # ExitStack owns every per-ATS CSV temp: Pass 1 streams each
        # enriched per-ATS slice into one of these, then Pass 3
        # ``scan_csv``s the same files (no re-enrichment) to build
        # the global all.parquet. Files are unlinked at function exit.
        with ExitStack() as stack:
            per_ats_csv_paths: dict[str, Path] = {}
            per_ats_entries: dict[ATSType, dict[str, object]] = {}
            schema_union: list[str] = []
            seen_cols: set[str] = set()
            tenant_status_paths = _find_tenant_status_files(source_dir, ats_csv_pattern)

            any_csv_found = False
            for ats in ATSType:
                if ats is ATSType.CUSTOM:
                    continue
                source_path = source_dir / ats_csv_pattern.format(ats=ats.value)
                if not source_path.exists():
                    continue
                if ats.value in RETIRED_SOURCES:
                    # The scheduled run no longer refreshes this file, so
                    # what a past run left behind would be published again
                    # on every run, a little more out of date each time.
                    logger.info(
                        "%s: source retired from scheduled ingestion; "
                        "its jobs file is not published.",
                        ats.value,
                    )
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
                # No provider has a jobs file, so there is no dataset to
                # publish. The per-company outcomes of the runs that failed
                # are still worth having: upload them before giving up.
                self._upload_tenant_status(tenant_status_paths)
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

            tenant_status_entry = self._upload_tenant_status(tenant_status_paths)
            if tenant_status_entry is not None:
                files_uploaded.extend(_collect_uploaded_keys(tenant_status_entry))

            jobs_by_kind = _jobs_by_source_kind(survivors)
            manifest_key, badges_key = self._patch_and_upload_manifest(
                generated_at=started,
                stats_factory=lambda existing: {
                    "total_jobs": n_kept,
                    "total_jobs_raw": n_raw,
                    # Postings from employers' own hiring systems (ATS and
                    # company career sites), after cross-source dedup. Public
                    # employment services and job boards are excluded.
                    "ats_jobs": sum(
                        n for kind, n in jobs_by_kind.items()
                        if kind in HEADLINE_KINDS
                    ),
                    "jobs_by_source_kind": jobs_by_kind,
                    "total_companies": _sum_by_ats_companies_rows(existing),
                    "ats_count": len(per_ats_entries),
                    "schema_version": "2.0",
                    "schema_columns": schema_union,
                },
                all_entry=all_entry,
                by_ats=per_ats_entries,
                existing_manifest=existing_manifest,
                tenant_status=tenant_status_entry,
            )
            if badges_key is not None:
                files_uploaded.append(badges_key)
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

        Three stages, all streaming:

        1. Per-ATS — ``scan_csv`` + ``semi``-join against its
           survivor index frame, sunk to a per-ATS temp parquet via
           ``sink_parquet``. Polars's semi-join on a small RHS is
           hash-probe, so the LHS streams.

        2. Merge parquet — the per-ATS temp parquets are concatenated
           into the global ``all.parquet`` via polars' lazy
           ``concat(diagonal_relaxed)`` + ``sink_parquet``, so
           heterogeneous per-ATS schemas are unified and peak memory
           is one Arrow batch.

        3. Convert to CSV — the merged parquet is re-scanned and
           ``sink_csv``'d for the ``all.csv`` artifact (~2.3 GB at
           current corpus size). Polars streams batches; nothing is
           materialized whole.
        """
        all_entry: dict[str, object] = {"rows": rows_total}

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

            # Build the merged parquet first — both ``all.parquet`` and
            # ``all.csv`` (when configured) source from this file so the
            # CSV path doesn't re-do the per-ATS semi-joins.
            all_pq = stage_stack.enter_context(_temp_file(".parquet"))
            if per_ats_parquets:
                _merge_parquets_streaming(per_ats_parquets, all_pq)
            else:
                pl.DataFrame(
                    schema=dict.fromkeys(schema_union, pl.String)
                ).write_parquet(all_pq, compression="zstd")

            if "parquet" in FORMATS_ALL and self._write_parquet:
                pq_key = f"{self._prefix}/all.parquet"
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

            if "csv" in FORMATS_ALL and self._write_all_csv:
                csv_key = f"{self._prefix}/all.csv"
                with _temp_file(".csv") as all_csv:
                    pl.scan_parquet(all_pq).sink_csv(all_csv)
                    csv_sha, csv_size = _file_sha_size(all_csv)
                    self._r2.upload(
                        all_csv,
                        csv_key,
                        content_type="text/csv",
                        cache_control=CACHE_CONTROL_LATEST,
                    )
                all_entry["csv"] = self._public_or_key(csv_key)
                # CSV's size + sha live in the canonical ``size_bytes``
                # / ``sha256`` slots (consumers default-fetching the
                # text format see the matching pair). Parquet keeps its
                # own ``parquet_*`` fields populated above.
                all_entry["size_bytes"] = csv_size
                all_entry["sha256"] = csv_sha

        return all_entry

    def _patch_and_upload_manifest(
        self,
        *,
        generated_at: datetime,
        stats_factory,
        all_entry: dict[str, object],
        by_ats: dict[ATSType, dict[str, object]],
        existing_manifest: dict[str, object] | None = None,
        tenant_status: dict[str, object] | None = None,
    ) -> tuple[str, str | None]:
        """Read existing manifest, replace jobs-related fields, preserve
        the companies block written by the CI.

        Returns the manifest key and the badges key, or ``None`` for the
        badges when their upload failed."""
        key = f"{self._prefix}/manifest.json"
        existing = (
            existing_manifest
            if existing_manifest is not None
            else _load_existing_manifest(self._r2, key)
        )

        manifest: dict[str, object] = {**existing}
        manifest["version"] = "2.0"
        manifest["generator"] = f"ats-scrapers/{__version__}"
        manifest["generated_at"] = generated_at.isoformat()
        # ``updated_at`` is the "manifest last touched" timestamp; both
        # writers (publisher + CI companies workflow) bump it so a
        # client like the homepage that reads only ``updated_at`` for
        # the freshness badge sees the latest write regardless of which
        # writer ran most recently. Format matches what the CI script
        # writes: UTC ``Z``-suffixed seconds.
        manifest["updated_at"] = generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        stats = stats_factory(existing)
        manifest["stats"] = stats
        manifest["all"] = all_entry
        manifest["by_ats"] = {ats.value: entry for ats, entry in by_ats.items()}
        if tenant_status is not None:
            manifest["tenant_status"] = tenant_status

        # Drop fields from the pre-2.0 layout if they survived the
        # legacy-path prune. Their data is gone so the entries point
        # nowhere.
        for legacy in ("by_date", "companies_by_ats"):
            manifest.pop(legacy, None)

        badges_key = self._upload_badges(stats, generated_at)
        body = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode(
            "utf-8"
        )
        self._r2.upload_bytes(
            body,
            key,
            content_type="application/json",
            cache_control=CACHE_CONTROL_LATEST,
        )
        return key, badges_key

    def _upload_badges(
        self, stats: dict[str, Any], generated_at: datetime
    ) -> str | None:
        """Upload the dataset's headline values already formatted.

        Shields' dynamic JSON badge can't format numbers. This file holds
        the values ready to display, so that the README badges can read
        them from here instead of the raw manifest.

        Returns the uploaded key. A failed upload is logged, not raised,
        and returns ``None``: badges are cosmetic and must not block the
        manifest.
        """
        badges = {
            "jobs": _compact(stats["total_jobs"]),
            "companies": _compact(stats["total_companies"]),
            "sources": str(stats["ats_count"]),
            "updated": generated_at.strftime("%Y-%m-%d"),
        }
        key = f"{self._prefix}/badges.json"
        try:
            self._r2.upload_bytes(
                json.dumps(badges).encode("utf-8"),
                key,
                content_type="application/json",
                cache_control=CACHE_CONTROL_LATEST,
            )
        except StorageError as exc:
            logger.warning("Badge upload failed: %s", exc)
            return None
        return key

    def _upload_tenant_status(self, paths: list[Path]) -> dict[str, object] | None:
        """Concatenate every provider's latest per-tenant scrape outcome
        into ``<prefix>/tenant_status.{csv,parquet}``."""
        frames: list[pl.DataFrame] = []
        for path in paths:
            try:
                frames.append(pl.read_csv(path, infer_schema_length=0))
            except (pl.exceptions.NoDataError, pl.exceptions.ComputeError) as exc:
                logger.warning("%s: unreadable tenant status (%s); skipping.", path, exc)
        if not frames:
            return None
        df = pl.concat(frames, how="diagonal").with_columns(
            pl.col("jobs").cast(pl.Int64, strict=False)
        )
        base_key = f"{self._prefix}/tenant_status"
        with _temp_file(".csv") as csv_path:
            df.write_csv(csv_path)
            entry, _ = self._upload_per_ats_streaming(
                csv_path=csv_path, base_key=base_key
            )
        return entry

    def _public_or_key(self, key: str) -> str:
        return self._r2.public_url(key) or key


# --- Cross-ATS dedup -------------------------------------------------------


# When the same (company, title, location) shows up under multiple ATSes,
# we keep the row from the highest-priority ATS (lowest number wins).
ATS_DEDUP_PRIORITY: dict[str, int] = {
    # Direct employer ATSes
    "adp": 1, "applitrack": 1, "ashby": 1, "avature": 1, "bamboohr": 1, "beisen": 1,
    "beisen_legacy": 1,
    "applicantpro": 1,
    "breezy": 1, "catsone": 1, "clearcompany": 1, "cornerstone": 1,
    "darwinbox": 1, "dayforce": 1,
    "feishu_hire": 1,
    "gem": 1,
    "greenhouse": 1, "gupy": 1, "herp": 1, "hrmos": 1, "icims": 1, "jazzhr": 1,
    "hireology": 1,
    "jibe": 1, "join_com": 1, "jobvite": 1, "keka": 1,
    "lever": 1,
    "ninehire": 1,
    "moka": 1, "oracle": 1, "pageup": 1, "paycom": 1, "paylocity": 1, "peopleadmin": 1, "personio": 1, "phenom": 1, "pinpoint": 1,
    "recruitee": 1,
    "recruiterbox": 1, "rippling": 1, "smartrecruiters": 1, "softgarden": 1,
    "successfactors": 1, "talentbrew": 1, "talentio": 1, "talentsoft": 1, "taleo": 1,
    "taleo_enterprise": 1, "varbi": 1,
    "teamtailor": 1, "ukg": 1,
    "workable": 1,
    "wintalent": 1,
    "workday": 1,
    # Big-tech bespoke careers — also priority 1 (single-tenant, canonical)
    "amazon": 1, "apple": 1, "bytedance": 1, "google": 1, "jd": 1, "meta": 1,
    "tesla": 1, "tiktok": 1, "uber": 1,
    # Hybrid jobboards
    "welcometothejungle": 3, "mercor": 3, "wantedly": 3,
    "seek": 4,
    # Sourcing/matching layer that mirrors others
    "eightfold": 5,
    # National public-sector aggregators — government-curated but the
    # same role often appears here AND on the employer's direct ATS.
    "bundesagentur": 6,
    "arbetsformedlingen": 6,
    "jobbankca": 6,
    "mycareersfuture": 6,
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


# Country-code map for the locations the EU aggregators (eures /
# bundesagentur / France Travail / arbetsformedlingen) emit. We don't
# need to recognise every country in the world — only the ones that
# show up in the duplicate-prone pairs. Anything we don't match falls
# through to ``""`` and Phase 1 / 2 dedup just skip that row.
_COUNTRY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DE", ("deutschland", "germany", "allemagne")),
    ("FR", ("france", "frankreich")),
    ("BE", ("belgique", "belgium", "belgien", "belgië")),
    ("AT", ("österreich", "austria", "autriche")),
    ("NL", ("nederland", "netherlands", "pays-bas", "niederlande")),
    ("IT", ("italia", "italy", "italien")),
    ("ES", ("españa", "spain", "espagne", "spanien")),
    ("PT", ("portugal",)),
    ("PL", ("polska", "poland", "pologne", "polen")),
    ("CH", ("schweiz", "suisse", "switzerland", "svizzera")),
    ("LU", ("luxembourg", "luxemburg")),
    ("DK", ("danmark", "denmark", "dänemark")),
    ("SE", ("sverige", "sweden", "schweden")),
    ("NO", ("norge", "norway", "norwegen")),
    ("FI", ("suomi", "finland", "finnland")),
    ("IE", ("ireland", "irlande", "irland")),
    ("CZ", ("česko", "czech", "tschechien")),
    ("US", ("united states", "u.s.a", "usa")),
    ("GB", ("united kingdom", "england", "scotland", "wales")),
    ("CA", ("canada",)),
)

# eures encodes the country as a 2-letter NUTS prefix followed by a
# parenthesised region code, e.g. ``"DE (DEA58)"`` / ``"FR (FRK21)"``.
# Matching just the two-letter prefix would false-positive on
# titles-as-locations like ``"Software Engineer, Remote"`` — we
# require the parens too. Case-insensitive: the pipeline lowercases
# ``location`` during harvest.
_NUTS_PREFIX_RE = re.compile(r"^\s*([a-z]{2})\s*\(", re.IGNORECASE)
_TRAILING_ISO_RE = re.compile(r"(?:^|[,\s(/])([a-z]{2})(?:[\s).]*)$", re.IGNORECASE)

# Word-boundary-anchored country needle patterns. Substring matching
# false-positived on common European place names — e.g. ``"usa"``
# inside ``"Lausanne"`` (CH) had Lausanne jobs ending up tagged as
# US. ``\b`` flanks every needle so the match requires non-word
# context on each side; the multi-character needles (``"u.s.a"``,
# ``"new zealand"``) still work because ``re.escape`` keeps the
# internal punctuation literal.
_COUNTRY_PATTERNS_RE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        code,
        re.compile(
            "|".join(rf"\b{re.escape(n)}\b" for n in needles),
            re.IGNORECASE,
        ),
    )
    for code, needles in _COUNTRY_PATTERNS
)
_COUNTRY_CODES = {country_code for country_code, _ in _COUNTRY_PATTERNS}


# Two of the trailing codes we accept are also US states. "san francisco, ca"
# is California and "wilmington, de" is Delaware: reading them as Canada and
# Germany put Californian jobs in Canadian blocks and published them with
# ``country_iso = CA``.
_CANADIAN_PROVINCES = frozenset(
    {
        "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt",
        "alberta", "british columbia", "manitoba", "new brunswick",
        "newfoundland and labrador", "nova scotia", "ontario", "quebec", "québec",
        "saskatchewan", "yukon",
    }
)  # fmt: skip
_CANADIAN_CITIES = frozenset(
    {
        "toronto", "vancouver", "montreal", "montréal", "calgary", "ottawa",
        "edmonton", "winnipeg", "mississauga", "quebec", "québec", "quebec city",
        "halifax", "waterloo", "kitchener", "burnaby", "markham", "brampton",
        "laval", "hamilton", "saskatoon", "regina", "surrey", "gatineau",
        "kelowna", "richmond hill", "oakville", "vaughan", "longueuil",
        "st. john's", "moncton", "fredericton",
    }
)  # fmt: skip
_DELAWARE_CITIES = frozenset(
    {
        "wilmington", "newark", "dover", "middletown", "new castle", "georgetown",
        "milford", "seaford", "lewes", "smyrna", "bear", "claymont",
        "rehoboth beach", "delaware city", "hockessin",
    }
)  # fmt: skip


def _trailing_code_country(lowered: str, code: str) -> str:
    """Country for a location that ends in the two-letter ``code``.

    ``ca`` is Canada when it stands alone or follows a Canadian province or
    a large Canadian city (``"toronto, on, ca"``), California otherwise;
    ``"ontario, ca"`` on its own is the Californian city.
    ``de`` is Germany unless it follows a Delaware town in a two-part
    location (``"wilmington, de"``).
    """
    parts = [part.strip() for part in lowered.split(",")]
    before = parts[-2] if len(parts) >= 2 else ""
    if code == "CA":
        if len(parts) == 2 and before == "ontario":
            return "US"  # the city of Ontario, California; the province follows a city
        if len(parts) == 1 or before in _CANADIAN_PROVINCES or before in _CANADIAN_CITIES:
            return "CA"
        return "US"
    if code == "DE" and len(parts) == 2 and before in _DELAWARE_CITIES:
        return "US"
    return code


def _country_iso_from_location(loc: object) -> str:
    """Heuristic ISO 3166-1 alpha-2 extraction from a free-form
    ``location`` string. Returns ``""`` when nothing matches.

    Covers the patterns observed across the duplicate-prone EU
    aggregators: full country names in DE/FR/EN, NUTS-region prefixes
    (``DE (DEA58)``), and ``"<City>, <Country>"`` suffixes.
    Word-boundary-anchored so ``"Lausanne"`` doesn't get tagged as US
    via a substring match on ``"usa"``.
    """
    if not isinstance(loc, str) or not loc.strip():
        return ""
    stripped = loc.strip()
    lowered = stripped.lower()
    for code, pat in _COUNTRY_PATTERNS_RE:
        if pat.search(lowered):
            return code
    m = _NUTS_PREFIX_RE.match(stripped)
    if m:
        return m.group(1).upper()
    m = _TRAILING_ISO_RE.search(stripped)
    if m:
        code = m.group(1).upper()
        if code in _COUNTRY_CODES:
            return _trailing_code_country(lowered, code)
    return ""


def _dedup_from_per_ats_csvs(
    per_ats_csv_paths: dict[str, Path],
) -> tuple[dict[str, pl.DataFrame], int, int]:
    """Build keys, run cross-ATS dedup, and return per-ATS survivors.

    The keys frame is sunk to a temp parquet via ``sink_parquet``
    (polars streaming write — peak memory bounded by one Arrow batch,
    not the corpus) before we run the eager dedup on it. This is the
    key memory win vs an in-memory ``pl.concat([..]).collect()``: the
    per-ATS scans are pulled in one ATS at a time, and the keys
    parquet on disk is small because only the thin deduplication
    columns are projected.

    Returns ``(survivors_by_ats, n_raw, n_kept)``.
    """
    if not per_ats_csv_paths:
        return {}, 0, 0

    key_lfs: list[pl.LazyFrame] = []
    for ats_value, csv_path in per_ats_csv_paths.items():
        scan = pl.scan_csv(csv_path, **_SCAN_CSV_KWARGS)
        schema_names = scan.collect_schema().names()
        # ``title_raw`` keeps the original (pre-strip-parens, pre-lower)
        # title so Phase 2 can compare titles with rapidfuzz over the
        # same text a human would compare.
        klf = scan.with_row_index(name="_local_idx").select(
            [
                pl.col("_local_idx").cast(pl.Int64),
                pl.lit(ats_value, dtype=pl.String).alias("ats_type"),
                pl.lit(
                    ATS_DEDUP_PRIORITY.get(ats_value, 2), dtype=pl.Int32
                ).alias("_priority"),
                _key_col_or_empty(schema_names, "url").alias("url"),
                _key_col_or_empty(schema_names, "title").alias("title_raw"),
                _key_col_or_empty(schema_names, "title")
                .str.to_lowercase()
                .alias("title"),
                _key_col_or_empty(schema_names, "company")
                .str.to_lowercase()
                .alias("company"),
                _key_col_or_empty(schema_names, "location")
                .str.to_lowercase()
                .alias("location"),
                _key_col_or_empty(schema_names, "country_iso")
                .str.to_uppercase()
                .alias("country_iso"),
                _key_col_or_empty(schema_names, "ats_id").alias("ats_id"),
                _key_col_or_empty(
                    schema_names, "requisition_id"
                ).alias("requisition_id"),
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
    *,
    fuzzy_threshold: int = 90,
    fuzzy_max_block_size: int = 5000,
) -> dict[str, pl.DataFrame]:
    """Run the six-pass cross-ATS dedup.

    Passes 1-4 are exact-match window-function passes (cheap). Passes 5
    and 6 compare titles inside one employer across sources: pass 5 the
    same title once case, accents, punctuation, gender tags and a
    closing bracket are set aside, pass 6 nearly the same title
    (rapidfuzz ``token_sort_ratio``). In both, the places must agree and
    a kept row absorbs at most one row per source and per place it
    lists, so 200 shops hiring under one title stay 200 postings. The
    rules live in ``pipeline/crossdedup.py``.

    The fuzzy pass is bounded by ``fuzzy_max_block_size`` (default
    5 000 rows per employer): a larger employer is compared country by
    country, and a country block that is still too large is skipped with
    a warning, so a recruiting agency with tens of thousands of postings
    doesn't blow up the wall clock with n² fuzz calls.

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

    # ---- Pass 3: cross-ATS (company_norm, requisition_id) dedup -----------
    work = work.with_columns(
        pl.col("company")
        .str.replace_all(r"[^a-z0-9]", "")
        .alias("_company_norm")
    )
    if "requisition_id" not in work.columns:
        work = work.with_columns(
            pl.lit("", dtype=pl.String).alias("requisition_id")
        )
    work = work.with_columns(
        (
            pl.col("_company_norm")
            + pl.lit("|")
            + pl.col("requisition_id")
        ).alias("_rid_key")
    )
    rid_valid = (
        (pl.col("_company_norm").str.len_bytes() > 0)
        & (pl.col("requisition_id").str.len_bytes() > 0)
    )
    n_ats_in_valid_rid = (
        pl.when(rid_valid)
        .then(pl.col("ats_type"))
        .otherwise(None)
        .n_unique()
        .over("_rid_key")
    )
    is_cross_rid = rid_valid & (n_ats_in_valid_rid > 1)
    winning_rid_ats = pl.col("ats_type").first().over("_rid_key")
    rid_keep = ~is_cross_rid | (
        pl.col("ats_type") == winning_rid_ats
    )
    work = work.filter(rid_keep).drop("_rid_key")

    # ---- Pass 4: cross-ATS (company_norm, ats_id) dedup -------------------
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
    winning_cid_ats = pl.col("ats_type").first().over("_cid_key")
    cid_keep = ~is_cross_cid | (
        pl.col("ats_type") == winning_cid_ats
    )
    work = work.filter(cid_keep).drop("_cid_key")

    # ---- Passes 5 and 6: same employer, same or nearly the same title ------
    # The country prefers the scraper's structured value and falls back to
    # the free-form ``location`` text (eures encodes it as the leading
    # ``DE``/``FR``/… token, Bundesagentur as a ``", Deutschland"`` suffix).
    # A row without a country is still compared: its place is then judged
    # on the city alone. See ``pipeline/crossdedup.py`` for the rules.
    structured_country = (
        pl.col("country_iso")
        if "country_iso" in work.columns
        else pl.lit("", dtype=pl.String)
    ).fill_null("").str.strip_chars()
    derived_country = pl.col("location").map_elements(
        _country_iso_from_location, return_dtype=pl.String
    )
    work = work.with_columns(
        pl.when(structured_country.str.len_bytes() > 0)
        .then(structured_country)
        .otherwise(derived_country)
        .alias("_country_iso"),
    )
    drop_orig_idxs = title_pass_drops(
        work,
        fuzzy_threshold=fuzzy_threshold,
        fuzzy_max_block_size=fuzzy_max_block_size,
    )
    if drop_orig_idxs:
        work = work.filter(~pl.col("_orig_idx").is_in(list(drop_orig_idxs)))

    work = work.drop("_company_norm", "_country_iso")

    # Materialize per-ATS survivor frames keyed on _local_idx for Pass 3
    # of the publish run.
    survivors: dict[str, pl.DataFrame] = {}
    parts = work.partition_by("ats_type", as_dict=True)
    for key_tuple, part in parts.items():
        ats_value = key_tuple[0] if isinstance(key_tuple, tuple) else key_tuple
        survivors[str(ats_value)] = part.select("_local_idx")
    return survivors


# --- helpers ---------------------------------------------------------------


def _enrich_lazy(lf: pl.LazyFrame) -> pl.LazyFrame:
    """Add ``is_remote`` / ``salary_min`` / ``salary_max`` / ``country_iso``
    columns when they aren't already present on the input.

    Implemented as polars expressions whenever possible so the lazy
    chain stays streamable through ``sink_csv``. ``is_remote`` reads
    the ``REMOTE_KEYWORDS`` and ``ONSITE_KEYWORDS`` lists from the
    canonical :mod:`ats_scrapers.enrichment.derived` module — both are
    optional, so a deploy that has narrowed the heuristic to title-only
    (no ``ONSITE_KEYWORDS`` exported) still gets a usable column.

    ``salary_summary`` and ``country_iso`` parsing both go through a
    Python callback (``map_elements``); polars' streaming engine
    doesn't run user functions, so the lazy chain falls back to the
    eager engine for an ATS slice that needs either. The country
    extractor was already used internally for Pass 1 dedup
    (``_country_iso_from_location``); exposing it as a public column
    means downstream consumers (D1 sync, R2 parquet readers) can
    filter / facet by ISO code without re-parsing the location string.
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

    if "location" in schema_names:
        derived_country = pl.col("location").map_elements(
            _country_iso_from_location, return_dtype=pl.String
        )
        if "country_iso" not in schema_names:
            lf = lf.with_columns(derived_country.alias("country_iso"))
        else:
            current_country = pl.col("country_iso").cast(pl.String)
            blank_country = current_country.is_null() | (
                current_country.str.strip_chars().str.len_bytes() == 0
            )
            missing_location = (
                pl.when(blank_country)
                .then(pl.col("location"))
                .otherwise(pl.lit(None, dtype=pl.String))
            )
            derived_missing = missing_location.map_elements(
                _country_iso_from_location,
                return_dtype=pl.String,
                skip_nulls=True,
            )
            lf = lf.with_columns(
                pl.when(blank_country)
                .then(derived_missing)
                .otherwise(current_country)
                .alias("country_iso")
            )

    return lf


def _is_remote_expr() -> pl.Expr:
    """Vectorized polars version of :func:`infer_is_remote`.

    Reads ``title`` (not ``location``) — the canonical heuristic
    in :mod:`ats_scrapers.enrichment.derived` is intentionally narrow and
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


def _find_tenant_status_files(source_dir: Path, ats_csv_pattern: str) -> list[Path]:
    """Status files of every provider, including those without a jobs.csv
    (a first run that failed still has outcomes worth publishing).

    The VPS publisher passes each jobs.csv as a symlink into a temp
    directory, so the real jobs root behind those symlinks is searched too.
    """
    depth = len(Path(ats_csv_pattern).parts)
    roots = [source_dir]
    for ats in ATSType:
        jobs_path = source_dir / ats_csv_pattern.format(ats=ats.value)
        if jobs_path.is_symlink():
            real_root = jobs_path.resolve().parents[depth - 1]
            if real_root not in roots:
                roots.append(real_root)
    found: list[Path] = []
    for ats in ATSType:
        if ats is ATSType.CUSTOM:
            continue
        for root in roots:
            status_path = (
                root / ats_csv_pattern.format(ats=ats.value)
            ).parent / TENANT_STATUS_FILENAME
            if status_path.is_file():
                found.append(status_path)
                break
    return found


def _collect_uploaded_keys(entry: dict[str, object]) -> list[str]:
    keys: list[str] = []
    for field_name in ("csv", "parquet"):
        value = entry.get(field_name)
        if isinstance(value, str):
            keys.append(value)
    return keys


def _jobs_by_source_kind(survivors: dict[str, pl.DataFrame]) -> dict[str, int]:
    """Count deduplicated jobs per source kind (see ``source_kind``)."""
    counts = {"ats": 0, "company_site": 0, "gov_board": 0, "job_board": 0}
    for ats_value, frame in survivors.items():
        counts[source_kind(ats_value)] += frame.height
    return counts


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


def _guard_suspicious_empty_job_slices(
    *,
    source_dir: Path,
    ats_csv_pattern: str,
    existing_manifest: dict[str, object],
) -> None:
    """Block publishes that would replace known-good provider data with empty.

    A header-only ``<ats>/jobs.csv`` is valid CSV, so the streaming publisher
    can otherwise upload it and patch the manifest to ``rows: 0``. Treat that
    as suspicious when the existing manifest proves the provider either had
    jobs before or still has tenants in ``by_ats_companies``.
    """
    if os.getenv("ATS_SCRAPERS_ALLOW_EMPTY_PUBLISH"):
        return

    by_ats = existing_manifest.get("by_ats")
    if not isinstance(by_ats, dict):
        by_ats = {}
    by_ats_companies = existing_manifest.get("by_ats_companies")
    if not isinstance(by_ats_companies, dict):
        by_ats_companies = {}

    suspicious: list[str] = []
    for ats in ATSType:
        # A retired source's file is not published, so an empty one cannot
        # replace anything, and no later run would refresh it.
        if ats is ATSType.CUSTOM or ats.value in RETIRED_SOURCES:
            continue
        source_path = source_dir / ats_csv_pattern.format(ats=ats.value)
        if not source_path.exists():
            continue

        previous_rows = _entry_rows(by_ats.get(ats.value))
        company_rows = _entry_rows(by_ats_companies.get(ats.value))
        has_prior_data = previous_rows > 0 or company_rows > 0
        if source_path.stat().st_size == 0:
            if has_prior_data:
                suspicious.append(
                    f"{ats.value}: local jobs.csv is 0 bytes; "
                    f"manifest previously had {previous_rows} jobs and "
                    f"{company_rows} companies. Suggested action: retry the "
                    "provider scrape or keep the previous published data."
                )
            continue
        if _csv_data_row_count(source_path) != 0:
            continue

        if has_prior_data:
            suspicious.append(
                f"{ats.value}: local jobs.csv has 0 rows; "
                f"manifest previously had {previous_rows} jobs and "
                f"{company_rows} companies. Suggested action: retry the "
                "provider scrape or keep the previous published data."
            )

    if suspicious:
        raise StorageError(
            "Refusing to publish suspicious empty provider slices. "
            "Set ATS_SCRAPERS_ALLOW_EMPTY_PUBLISH=1 only for intentional empty "
            "providers.\n- "
            + "\n- ".join(suspicious)
        )


def _compact(n: int) -> str:
    """Same output as stapply.ai's ``Intl.NumberFormat`` compact notation,
    e.g. 4886484 -> "4.9M"."""
    # Decimal, rounded half up: a float's ``:.1f`` rounds ties to even and
    # turns 1,250 into "1.2K" where the site shows "1.3K".
    value, suffix = Decimal(n), ""
    for next_suffix in ("K", "M", "B"):
        if _one_decimal(value) < 1000:
            break
        value /= 1000
        suffix = next_suffix
    return f"{_one_decimal(value):f}".removesuffix(".0") + suffix


def _one_decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _entry_rows(entry: object) -> int:
    if not isinstance(entry, dict):
        return 0
    rows = entry.get("rows")
    return rows if isinstance(rows, int) and rows > 0 else 0


def _csv_data_row_count(path: Path) -> int:
    with path.open("rb") as f:
        lines = sum(1 for _ in f)
    return max(lines - 1, 0)


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
