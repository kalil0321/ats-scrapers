"""Publish a directory of per-ATS CSVs to Cloudflare R2 as a versioned dataset.

Layout produced under `<prefix>` (default `jobhive/v1`):

    jobhive/v1/manifest.json
    jobhive/v1/jobs/all.parquet              # full snapshot — parquet only
    jobhive/v1/jobs/by-ats/<ats>.parquet     # per-ATS slice (parquet + csv)
    jobhive/v1/jobs/by-ats/<ats>.csv
    jobhive/v1/jobs/by-date/<YYYY-MM-DD>.parquet  (immutable, versioned)
    jobhive/v1/companies/all.csv             # global slug→ATS mapping
    jobhive/v1/companies/by-ats/<ats>.csv    # per-ATS company list

The manifest is uploaded last so a half-finished run never points at missing
files.

Source preference: when a `dated_snapshots` argument is provided, the latest
one is used as the source of truth for `all` and `by-ats` (it has the rich
14-column enriched schema). Otherwise we fall back to concatenating the thin
per-ATS `<ats>/jobs.csv` files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
    from collections.abc import Iterable

    from jobhive.storage.r2 import R2Client

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "jobhive/v1"
CACHE_CONTROL_LATEST = "public, max-age=300"  # 5 min — manifest + latest files
CACHE_CONTROL_DATED = "public, max-age=31536000, immutable"  # historical files

# Format choices per artifact type.
# `all` is parquet-only because it's the largest file (CSV would be ~150 MB).
FORMATS_ALL = ("parquet",)
FORMATS_PER_ATS = ("csv", "parquet")
FORMATS_DATED = ("parquet",)
FORMATS_COMPANIES = ("csv",)


@dataclass
class PublishResult:
    """Summary of what was uploaded in a single publish run."""

    manifest_key: str
    files: list[str] = field(default_factory=list)
    total_jobs: int = 0
    total_companies: int = 0
    duration_seconds: float = 0.0


class DatasetPublisher:
    """Builds and publishes a versioned dataset to R2."""

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

    def prune_old_dated_snapshots(self, keep_date: str) -> int:
        """Delete every `jobs/by-date/*` object whose date is not `keep_date`."""
        prefix = f"{self._prefix}/jobs/by-date/"
        keys_to_delete: list[str] = []
        for obj in self._r2.list(prefix=prefix):
            key = obj.get("Key", "")
            stem = key[len(prefix) :].rsplit(".", 1)[0]
            if stem and stem != keep_date:
                keys_to_delete.append(key)
        if not keys_to_delete:
            return 0
        return self._r2.delete_many(keys_to_delete)

    def publish_from_directory(
        self,
        source_dir: Path,
        *,
        ats_csv_pattern: str = "{ats}/jobs.csv",
        dated_snapshots: Iterable[Path] | None = None,
        companies_csv: Path | None = None,
    ) -> PublishResult:
        """Publish jobs from a local directory.

        If `dated_snapshots` is non-empty, the most-recent file becomes the
        source for `all` and `by-ats` slices (it has the enriched schema).
        Otherwise we concatenate the thin per-ATS files.

        `companies_csv` is ignored — companies are now built from each ATS's
        `<ats>/<ats>_companies.csv` file directly.
        """
        del companies_csv  # built per-ATS now
        started = datetime.now(UTC)
        files_uploaded: list[str] = []

        dated_list = sorted(dated_snapshots or [], key=_date_sort_key, reverse=True)
        # The per-ATS thin CSVs are the spine — they cover the full dataset.
        # Dated snapshots are typically a niche subset (e.g. AI-filtered) and
        # carry richer columns; we left-join them in for the rows they touch.
        full_df = _concat_thin_per_ats(source_dir, ats_csv_pattern)
        logger.info("Concatenated %d rows from %d per-ATS files", len(full_df),
                    full_df["ats_type"].nunique() if "ats_type" in full_df.columns else 0)

        if dated_list:
            latest = dated_list[0]
            logger.info("Enriching with latest dated snapshot: %s", latest)
            full_df = _left_join_enrichment(full_df, _read_csv_safely(latest))

        full_df = _enrich_with_derived(full_df)

        # Per-ATS slices = filter the enriched dataframe (un-deduped — within
        # one ATS, the per-tenant URL key already prevents duplicates).
        per_ats_entries: dict[ATSType, dict[str, object]] = {}
        for ats in ATSType:
            if ats is ATSType.CUSTOM:
                continue
            mask = full_df.get("ats_type")
            if mask is None:
                continue
            slice_df = full_df[mask == ats.value]
            if slice_df.empty:
                continue
            entry = self._upload_dataframe(
                slice_df,
                base_key=f"{self._prefix}/jobs/by-ats/{ats.value}",
                versioned=False,
                formats=FORMATS_PER_ATS,
            )
            per_ats_entries[ats] = entry
            files_uploaded.extend(_collect_uploaded_keys(entry))

        # Cross-ATS deduplication for the global snapshot. Some companies post
        # the same role through multiple ATSes (e.g. Eightfold mirroring an
        # underlying Workday). The per-ATS slices keep raw counts so consumers
        # querying a specific ATS see exactly what that ATS exposes; the "all"
        # snapshot is the unique-jobs canonical view.
        full_df_deduped = _dedupe_cross_ats(full_df)
        logger.info(
            "Cross-ATS dedup: %d → %d rows (%d duplicates removed)",
            len(full_df),
            len(full_df_deduped),
            len(full_df) - len(full_df_deduped),
        )

        # Full snapshot — parquet only
        all_entry = self._upload_dataframe(
            full_df_deduped,
            base_key=f"{self._prefix}/jobs/all",
            versioned=False,
            formats=FORMATS_ALL,
        )
        files_uploaded.extend(_collect_uploaded_keys(all_entry))

        # Dated snapshots
        per_date_entries: dict[str, dict[str, object]] = {}
        for snap_path in dated_list:
            date_key = _extract_date_from_filename(snap_path.name)
            if date_key is None:
                logger.warning("Skipping %s — no parseable date in filename", snap_path)
                continue
            df = _enrich_with_derived(_read_csv_safely(snap_path))
            entry = self._upload_dataframe(
                df,
                base_key=f"{self._prefix}/jobs/by-date/{date_key}",
                versioned=True,
                formats=FORMATS_DATED,
            )
            per_date_entries[date_key] = entry
            files_uploaded.extend(_collect_uploaded_keys(entry))

        # Companies — global + per-ATS
        companies_master_df = _build_companies_master(source_dir, full_df)
        companies_entry: dict[str, object] | None = None
        if not companies_master_df.empty:
            companies_entry = self._upload_dataframe(
                companies_master_df,
                base_key=f"{self._prefix}/companies/all",
                versioned=False,
                formats=FORMATS_COMPANIES,
            )
            files_uploaded.extend(_collect_uploaded_keys(companies_entry))

        companies_per_ats: dict[ATSType, dict[str, object]] = {}
        for ats in ATSType:
            if ats is ATSType.CUSTOM:
                continue
            slice_df = companies_master_df[companies_master_df["ats"] == ats.value]
            if slice_df.empty:
                continue
            entry = self._upload_dataframe(
                slice_df,
                base_key=f"{self._prefix}/companies/by-ats/{ats.value}",
                versioned=False,
                formats=FORMATS_COMPANIES,
            )
            companies_per_ats[ats] = entry
            files_uploaded.extend(_collect_uploaded_keys(entry))

        manifest_key = self._upload_manifest(
            generated_at=started,
            stats={
                # ``total_jobs`` is the deduped count — what an end user
                # actually sees. ``total_jobs_raw`` is the pre-dedup sum
                # for transparency.
                "total_jobs": len(full_df_deduped),
                "total_jobs_raw": len(full_df),
                # ``total_companies`` is the headline figure: companies
                # with at least one active job in the published dataset.
                # ``total_companies_tracked`` is the broader catalog of
                # every tenant we've discovered across every ATS (even
                # those currently posting zero jobs). Exposing both
                # explicitly lets the frontend say
                # "X companies with active jobs / Y tracked" without
                # having to reconcile two different files.
                "total_companies": int(full_df_deduped["company"].nunique())
                if "company" in full_df_deduped.columns
                else 0,
                "total_companies_tracked": int(len(companies_master_df))
                if not companies_master_df.empty
                else 0,
                "total_companies_with_jobs": int(
                    (companies_master_df["total_jobs"] > 0).sum()
                ) if "total_jobs" in companies_master_df.columns else 0,
                "ats_count": len(per_ats_entries),
                "schema_version": "2.0",
                "schema_columns": list(full_df_deduped.columns),
            },
            all_entry=all_entry,
            by_ats=per_ats_entries,
            by_date=per_date_entries,
            companies=companies_entry,
            companies_by_ats=companies_per_ats,
        )
        files_uploaded.append(manifest_key)

        ended = datetime.now(UTC)
        return PublishResult(
            manifest_key=manifest_key,
            files=files_uploaded,
            total_jobs=len(full_df_deduped),
            total_companies=int(full_df_deduped["company"].nunique())
            if "company" in full_df_deduped.columns
            else 0,
            duration_seconds=(ended - started).total_seconds(),
        )

    def _upload_dataframe(
        self,
        df: pd.DataFrame,
        *,
        base_key: str,
        versioned: bool,
        formats: tuple[str, ...],
    ) -> dict[str, object]:
        cache = CACHE_CONTROL_DATED if versioned else CACHE_CONTROL_LATEST
        entry: dict[str, object] = {"rows": len(df)}

        if "csv" in formats:
            csv_key = f"{base_key}.csv"
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            self._r2.upload_bytes(
                csv_bytes, csv_key, content_type="text/csv", cache_control=cache
            )
            entry["csv"] = self._public_or_key(csv_key)
            entry["size_bytes"] = len(csv_bytes)
            entry["sha256"] = hashlib.sha256(csv_bytes).hexdigest()

        if "parquet" in formats and self._write_parquet:
            from io import BytesIO

            buffer = BytesIO()
            _normalize_for_parquet(df).to_parquet(buffer, index=False, compression="zstd")
            parquet_bytes = buffer.getvalue()
            parquet_key = f"{base_key}.parquet"
            self._r2.upload_bytes(
                parquet_bytes,
                parquet_key,
                content_type="application/vnd.apache.parquet",
                cache_control=cache,
            )
            entry["parquet"] = self._public_or_key(parquet_key)
            entry["parquet_size_bytes"] = len(parquet_bytes)
            if "size_bytes" not in entry:
                entry["size_bytes"] = len(parquet_bytes)
                entry["sha256"] = hashlib.sha256(parquet_bytes).hexdigest()

        return entry

    def _upload_manifest(
        self,
        *,
        generated_at: datetime,
        stats: dict[str, object],
        all_entry: dict[str, object],
        by_ats: dict[ATSType, dict[str, object]],
        by_date: dict[str, dict[str, object]],
        companies: dict[str, object] | None,
        companies_by_ats: dict[ATSType, dict[str, object]],
    ) -> str:
        manifest = {
            "version": "1.0",
            "generator": f"jobhive/{__version__}",
            "generated_at": generated_at.isoformat(),
            "stats": stats,
            "all": all_entry,
            "by_ats": {ats.value: entry for ats, entry in by_ats.items()},
            "by_date": by_date,
            "companies": companies,
            "companies_by_ats": {
                ats.value: entry for ats, entry in companies_by_ats.items()
            },
        }
        key = f"{self._prefix}/manifest.json"
        body = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode("utf-8")
        self._r2.upload_bytes(
            body,
            key,
            content_type="application/json",
            cache_control=CACHE_CONTROL_LATEST,
        )
        return key

    def _public_or_key(self, key: str) -> str:
        return self._r2.public_url(key) or key


# --- helpers -----------------------------------------------------------------


# When the same (company, title, location) shows up under multiple ATSes,
# we keep the row from the highest-priority ATS (lowest number wins).
#
# Priority 1 = direct employer ATS (employer hosts the posting natively).
# Priority 2 = unknown / future ATSes (default).
# Priority 3 = hybrid jobboards (companies post directly, but board adds
#              its own metadata layer).
# Priority 5 = sourcing/matching layer that mirrors an underlying ATS
#              (e.g. Eightfold scraping its customers' Workday).
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
    # Lowest priority so cross-ATS dedup keeps the employer-direct row.
    "bundesagentur": 6,
    "arbetsformedlingen": 6,
    "usajobs": 6,
}


def _dedupe_cross_ats(df: pd.DataFrame) -> pd.DataFrame:
    """Conservative deduplication for the global ``all`` snapshot.

    Three passes, each narrow on purpose so distinct postings aren't collapsed:

    1.  **Exact-URL dedup.** Rows sharing a non-empty ``url`` are the same
        listing — keep the highest-priority ATS (per ``ATS_DEDUP_PRIORITY``).
        Also catches within-ATS URL collisions (e.g. Workable's
        ``apply.workable.com/j/{shortcode}`` URLs are tenant-agnostic, so
        the same job posted by two related boards yields the same URL —
        ~31k Workable rows folded out this way).

    2.  **Cross-ATS ``(company, title, location)`` dedup, ONLY when the
        group spans >1 ATS.** Two openings inside *one* ATS sharing
        ``(c,t,l)`` are usually distinct reqs (multiple SWE roles in
        Paris), so we leave them. But the same tuple under *different*
        ATSes is almost always one role being mirrored — collapse to the
        highest-priority row.

    3.  **Cross-ATS ``(company, title)`` dedup, ONLY when the group spans
        >1 ATS AND the company actually matches.** Catches location-format
        variants — same role posted as "Remote" on one ATS and
        "Remote — United States" on another. Empirically ~5k cases. Still
        scoped to cross-ATS only, so within-ATS distinct openings (5
        SWE-Paris reqs at AcmeCorp on Workday) are preserved.

    Rows with blank company or title are passed through untouched. Original
    row order is preserved when nothing collapses.

    The earliest version of this function used the ``(c,t,l)`` key
    universally; an empirical audit on ~1.2M rows showed that strategy
    collapsed ~220k distinct within-ATS openings and only ~70 real
    cross-ATS mirror cases. The current cross-ATS-only scoping keeps the
    coverage and stops eating the within-ATS tail.
    """
    if df.empty or "ats_type" not in df.columns:
        return df
    if not {"title", "company"}.issubset(df.columns):
        return df

    work = df.reset_index(drop=False).rename(columns={"index": "_orig_idx"})
    work["_priority"] = (
        work["ats_type"].astype(str).map(ATS_DEDUP_PRIORITY).fillna(2).astype(int)
    )

    # ---- Pass 1: exact-URL dedup ------------------------------------------
    if "url" in work.columns:
        url_norm = work["url"].fillna("").astype(str).str.strip()
        has_url = url_norm.str.len() > 0
        with_url = work.loc[has_url].sort_values(
            ["_priority", "_orig_idx"], kind="stable"
        )
        with_url_dedup = with_url.drop_duplicates(subset=["url"], keep="first")
        without_url = work.loc[~has_url]
        work = pd.concat([with_url_dedup, without_url], ignore_index=False)

    # ---- Pass 2: cross-ATS (company, title, location) dedup ---------------
    title_n = work["title"].fillna("").astype(str).str.strip().str.lower()
    company_n = work["company"].fillna("").astype(str).str.strip().str.lower()
    if "location" in work.columns:
        location_n = work["location"].fillna("").astype(str).str.strip().str.lower()
    else:
        location_n = pd.Series([""] * len(work), index=work.index)
    work["_dedup_key"] = company_n + "|" + title_n + "|" + location_n

    valid_mask = (company_n.str.len() > 0) & (title_n.str.len() > 0)
    invalid_kept = work.loc[~valid_mask]
    valid = work.loc[valid_mask]

    # Find dedup keys spanning >1 distinct ATS — those are the cross-ATS
    # collisions worth collapsing. Single-ATS groups stay as-is.
    ats_per_key = valid.groupby("_dedup_key")["ats_type"].transform("nunique")
    cross_ats_keys = ats_per_key > 1

    cross_kept = (
        valid.loc[cross_ats_keys]
        .sort_values(["_priority", "_orig_idx"], kind="stable")
        .drop_duplicates(subset=["_dedup_key"], keep="first")
    )
    within_passthrough = valid.loc[~cross_ats_keys]

    # ---- Pass 3: cross-ATS (company, ats_id) dedup -------------------------
    # Eightfold often sits on top of Workday/iCIMS for the same employer
    # and exposes the BACKING ATS req id as ``ats_id`` (e.g. PayPal:
    # eightfold.ats_id == workday.ats_id == "R0136150"). The (c,t,l) key
    # misses these because Eightfold formats locations like
    # "San Francisco, CA, US" while Workday says "San Francisco" — same
    # role, different string. Matching on the *requisition id* under the
    # same employer avoids that pitfall and never collapses two genuinely
    # different roles (an ats_id is unique per req at the source).
    #
    # Empirical: catches ~7.5k duplicates (paypal/nvidia/micron/dexcom/
    # ptc/trimble), zero false positives in our sample.
    survivors = pd.concat([cross_kept, within_passthrough], ignore_index=False)
    company_norm_s = (
        survivors["company"].fillna("").astype(str).str.lower()
        .str.replace(r"[^a-z0-9]", "", regex=True)
    )
    ats_id_s = survivors["ats_id"].fillna("").astype(str).str.strip() if "ats_id" in survivors.columns else None
    if ats_id_s is not None:
        survivors["_cid_key"] = company_norm_s + "|" + ats_id_s
        # Only fire when ats_id is non-empty AND company is non-empty.
        cid_valid = (company_norm_s.str.len() > 0) & (ats_id_s.str.len() > 0)
        ats_count = (
            survivors.loc[cid_valid]
            .groupby("_cid_key")["ats_type"]
            .transform("nunique")
        )
        cross_cid_idx = ats_count[ats_count > 1].index
        cid_kept = (
            survivors.loc[cross_cid_idx]
            .sort_values(["_priority", "_orig_idx"], kind="stable")
            .drop_duplicates(subset=["_cid_key"], keep="first")
        )
        non_cross_cid = survivors.loc[~survivors.index.isin(cross_cid_idx)]
        survivors = pd.concat([cid_kept, non_cross_cid], ignore_index=False)
        survivors = survivors.drop(columns=["_cid_key"], errors="ignore")

    out = pd.concat([survivors, invalid_kept], ignore_index=False)
    out = out.sort_values("_orig_idx", kind="stable").drop(
        columns=["_orig_idx", "_dedup_key", "_priority"]
    )
    return out.reset_index(drop=True)


def _enrich_with_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add `is_remote`, `salary_min/max` columns derived from existing
    data."""
    df = df.copy()
    if "location" in df.columns and "is_remote" not in df.columns:
        df["is_remote"] = df["location"].apply(infer_is_remote)
    if "salary_summary" in df.columns and "salary_min" not in df.columns:
        parsed = df["salary_summary"].apply(parse_salary_range)
        df["salary_min"] = parsed.apply(lambda t: t[0])
        df["salary_max"] = parsed.apply(lambda t: t[1])
    return df


def _left_join_enrichment(spine: pd.DataFrame, enriched: pd.DataFrame) -> pd.DataFrame:
    """Left-join `enriched` columns onto `spine` on the `url` key.

    Existing columns in `spine` win; only new columns from `enriched` are
    pulled in. Rows in `spine` without a match in `enriched` keep the spine's
    values and have NaN for the new columns.
    """
    if "url" not in spine.columns or "url" not in enriched.columns:
        return spine
    new_cols = [c for c in enriched.columns if c not in spine.columns]
    if not new_cols:
        return spine
    return spine.merge(
        enriched[["url", *new_cols]].drop_duplicates(subset=["url"], keep="last"),
        on="url",
        how="left",
    )


def _concat_thin_per_ats(source_dir: Path, ats_csv_pattern: str) -> pd.DataFrame:
    """Fallback: concatenate the per-ATS thin CSVs into one frame."""
    slices: list[pd.DataFrame] = []
    for ats in ATSType:
        if ats is ATSType.CUSTOM:
            continue
        path = source_dir / ats_csv_pattern.format(ats=ats.value)
        if not path.exists():
            continue
        df = _read_csv_safely(path)
        df["ats_type"] = ats.value
        slices.append(df)
    if not slices:
        raise StorageError(f"No ATS CSVs found in {source_dir}")
    return pd.concat(slices, ignore_index=True)


# Per-ATS regex extracting the *tenant slug* from a job posting URL.
# The tenant slug is the stable identifier the scraper uses (and that the
# corresponding `{ats}_companies.csv` keys on). Joining `(ats_type,
# tenant_slug_from_url)` against `(ats, csv_name_or_slug)` is far more reliable
# than joining on the human-readable ``company`` field, which varies wildly:
# Greenhouse stores it as a numeric board id, Workday/Phenom/Taleo store the
# real company name (e.g. "Edward Snell & Co"), Oracle stores the API host,
# etc.
_TENANT_SLUG_FROM_URL: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(r"https?://(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)"),
    "lever": re.compile(r"https?://jobs\.lever\.co/([^/?#]+)"),
    "ashby": re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)"),
    # Workable URLs hide the tenant: ``apply.workable.com/j/{job_id}`` carries
    # only the job id. There's no slug to extract — use the ``company`` field
    # in jobs.csv as the join key (handled by the fallback path).
    # Intentional: do NOT add a workable regex here.
    # The first path segment is the tenant slug; subsequent segments are the
    # job id.
    "smartrecruiters": re.compile(r"https?://jobs\.smartrecruiters\.com/([^/?#]+)"),
    "workday": re.compile(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com"),
    "icims": re.compile(r"https?://(?:careers|uscareers)-([^.]+)\.icims\.com"),
    "oracle": re.compile(r"https?://([^/]+\.oraclecloud\.com)"),
    "breezy": re.compile(r"https?://([^.]+)\.breezy\.hr"),
    "cornerstone": re.compile(r"https?://([^.]+)\.csod\.com"),
    "personio": re.compile(r"https?://([^.]+)\.jobs\.personio\.com"),
    "rippling": re.compile(r"https?://ats\.rippling\.com/([^/?#]+)"),
    "bamboohr": re.compile(r"https?://([^.]+)\.bamboohr\.com"),
    "teamtailor": re.compile(r"https?://([^.]+)\.teamtailor\.com"),
    "recruitee": re.compile(r"https?://([^.]+)\.recruitee\.com"),
    "jazzhr": re.compile(r"https?://([^.]+)\.applytojob\.com"),
    "join_com": re.compile(r"https?://join\.com/companies/([^/?#]+)"),
    "pinpoint": re.compile(r"https?://([^.]+)\.pinpointhq\.com"),
    "recruiterbox": re.compile(r"https?://([^.]+)\.hire\.trakstar\.com"),
    "eightfold": re.compile(
        r"https?://(?:apply\.careers\.([^.]+)\.com|([^.]+)\.eightfold\.ai)"
    ),
    # Phenom posts URL hosts vary per tenant ("jobs.bell.ca", "jobs.target.com").
    # Extract the host as a stable slug.
    "phenom": re.compile(r"https?://(jobs\.[^/?#]+)"),
    # Taleo URLs embed `org=XYZ` in the query string. The org code is the
    # stable per-tenant slug; both jobs.csv URLs and companies.csv search
    # URLs carry the same `org=` value.
    "taleo": re.compile(r"[?&]org=([^&]+)"),
    # SuccessFactors posts under custom company hosts ("job.{co}.com").
    "successfactors": re.compile(r"https?://(job\.[^/?#]+)"),
    # Avature posts under per-tenant hosts; use the host as the slug.
    "avature": re.compile(r"https?://([^/]+)/"),
}


def _normalize_slug(value: str) -> str:
    """Lowercase + strip whitespace + drop non-alphanumeric characters.

    Used to align CSV ``name`` values (which can be CamelCase or contain
    spaces/dashes) with URL-derived tenant slugs (which are typically lowercase
    alphanumeric)."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _count_jobs_per_tenant(full_df: pd.DataFrame) -> dict[tuple[str, str], int]:
    """Group rows by ``(ats_type, normalized_tenant_slug)`` and count.

    The tenant slug is derived from the job URL via per-ATS regex. Returns a
    map keyed on normalized slug so the join in ``_build_companies_master``
    is case- and punctuation-tolerant.
    """
    if not {"ats_type", "url"}.issubset(full_df.columns):
        return {}

    counts: dict[tuple[str, str], int] = {}
    for ats_value, group in full_df.groupby("ats_type"):
        pattern = _TENANT_SLUG_FROM_URL.get(str(ats_value))
        if pattern is None:
            # Big-tech custom ATSes (amazon/apple/google/...) and ATSes
            # without a URL pattern: fall back to the ``company`` field.
            if "company" in group.columns:
                fallback = group.groupby("company").size()
                for company, n in fallback.items():
                    if pd.notna(company):
                        counts[(str(ats_value), _normalize_slug(company))] = int(n)
            continue
        urls = group["url"].dropna().astype(str)
        for url in urls:
            m = pattern.search(url)
            if not m:
                continue
            # The first non-empty captured group is the slug. Eightfold has
            # an OR with two groups; only one matches per URL.
            slug = next((g for g in m.groups() if g), None) or m.group(1)
            key = (str(ats_value), _normalize_slug(slug))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _build_companies_master(source_dir: Path, full_df: pd.DataFrame) -> pd.DataFrame:
    """Build a unified companies table from each ATS's `<ats>_companies.csv`.

    Produces columns: `slug`, `name`, `ats`, `careers_url`, `total_jobs`.
    `total_jobs` is derived from URL-based tenant inference (see
    ``_count_jobs_per_tenant``) — joining on the user-facing ``company``
    field is unreliable (it can be a board id, an API name, an ATS host,
    or the human-readable company name depending on the platform).
    """
    job_counts = _count_jobs_per_tenant(full_df)

    rows: list[dict[str, object]] = []
    for ats in ATSType:
        if ats is ATSType.CUSTOM:
            continue
        path = _find_companies_file(source_dir, ats.value)
        if not path:
            continue
        df = _read_csv_safely(path)
        for _, row in df.iterrows():
            slug = str(row.get("name") or row.get("slug") or "").strip()
            url = str(row.get("url") or "").strip() or None
            if not slug:
                continue
            # Try the slug first; if no match, derive from the URL (covers
            # CSVs where `name` is human-readable but `url` carries the slug).
            normalized = _normalize_slug(slug)
            count = job_counts.get((ats.value, normalized), 0)
            if count == 0 and url:
                pattern = _TENANT_SLUG_FROM_URL.get(ats.value)
                if pattern is not None:
                    m = pattern.search(url)
                    if m:
                        url_slug = next((g for g in m.groups() if g), None) or m.group(1)
                        count = job_counts.get(
                            (ats.value, _normalize_slug(url_slug)), 0
                        )
            rows.append(
                {
                    "slug": slug,
                    "name": slug,
                    "ats": ats.value,
                    "careers_url": url,
                    "total_jobs": count,
                }
            )
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["slug", "name", "ats", "careers_url", "total_jobs"]
    )


def _find_companies_file(source_dir: Path, ats: str) -> Path | None:
    candidates = [
        source_dir / ats / f"{ats}_companies.csv",
        source_dir / ats / "companies.csv",
        source_dir / f"{ats}_companies.csv",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _normalize_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Cast object-dtype columns to typed nullable for parquet stability.

    `object` columns containing only True/False/None values become nullable
    booleans; everything else becomes nullable string. This keeps the
    `is_remote` column queryable as `df.is_remote == True` instead of `"True"`.
    """
    df = df.copy()
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


def _extract_date_from_filename(name: str) -> str | None:
    """Extract YYYY-MM-DD from filenames like `ai-03-05-2026.csv`."""
    import re

    match = re.search(r"(\d{2})-(\d{2})-(\d{4})", name)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return None


def _date_sort_key(path: Path) -> str:
    """Sort dated snapshots so `max(...)` returns the most recent date."""
    return _extract_date_from_filename(path.name) or ""
