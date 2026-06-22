"""jobhive CLI — `jobhive --help`.

Surface area is intentionally small: search, scrape, publish, list-ats. Anything
more involved should drop into Python (this is a library first, CLI second).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from jobhive._version import __version__
from jobhive.exceptions import JobHiveError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jobhive",
        description="Open dataset and toolkit for job market data.",
    )
    parser.add_argument("--version", action="version", version=f"jobhive {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_search = subparsers.add_parser("search", help="Query the public dataset")
    p_search.add_argument("query", nargs="?", help="Title substring (case-insensitive)")
    p_search.add_argument("--location", help="Location substring")
    p_search.add_argument("--company", help="Company substring")
    p_search.add_argument("--ats", help="Restrict to one ATS slice")
    p_search.add_argument(
        "--csv",
        "--prefer-csv",
        action="store_true",
        dest="prefer_csv",
        help="Prefer CSV artifacts instead of Parquet.",
    )
    p_search.add_argument("--remote", action="store_true", help="Remote jobs only")
    p_search.add_argument("--salary-min", type=float, dest="salary_min")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
    )

    p_scrape = subparsers.add_parser("scrape", help="Scrape one company from one ATS")
    p_scrape.add_argument("ats", help="ATS platform (greenhouse, lever, ashby, ...)")
    p_scrape.add_argument("company", help="Company slug on the ATS")
    p_scrape.add_argument("--format", choices=["table", "csv", "json"], default="table")

    p_publish = subparsers.add_parser("publish", help="Publish a directory of CSVs to R2")
    p_publish.add_argument("source_dir", help="Directory containing per-ATS CSVs")
    p_publish.add_argument(
        "--pattern",
        default="{ats}/jobs.csv",
        help="Path template under source_dir (default: {ats}/jobs.csv)",
    )
    p_publish.add_argument("--companies-csv", help="Optional companies.csv path")
    p_publish.add_argument(
        "--dated-glob",
        help="Optional glob (relative to source_dir) for dated daily snapshots",
    )
    p_publish.add_argument(
        "--no-parquet", action="store_true", help="Skip parquet writes (CSV only)"
    )

    subparsers.add_parser("list-ats", help="List ATS platforms with available data")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.command == "search":
            return _cmd_search(args)
        if args.command == "scrape":
            return _cmd_scrape(args)
        if args.command == "publish":
            return _cmd_publish(args)
        if args.command == "list-ats":
            return _cmd_list_ats()
    except JobHiveError as exc:
        print(f"jobhive: error: {exc}", file=sys.stderr)
        return 1
    return 1


def _cmd_search(args: argparse.Namespace) -> int:
    from jobhive.client import Client

    client = Client(prefer_parquet=False if args.prefer_csv else None)
    df = client.search(
        query=args.query,
        location=args.location,
        company=args.company,
        ats=args.ats,
        remote=args.remote or None,
        salary_min=args.salary_min,
        limit=args.limit,
    )
    _emit(df, args.format)
    return 0


def _cmd_scrape(args: argparse.Namespace) -> int:
    import pandas as pd

    from jobhive.scrapers import get_scraper

    scraper = get_scraper(args.ats, args.company)
    jobs = scraper.fetch()
    df = pd.DataFrame([j.model_dump(mode="json") for j in jobs])
    _emit(df, args.format)
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from jobhive.storage import DatasetPublisher, R2Client, R2Config

    source = Path(args.source_dir).expanduser().resolve()
    config = R2Config.from_env()
    r2 = R2Client(config)
    publisher = DatasetPublisher(r2, write_parquet=not args.no_parquet)

    dated: list[Path] = []
    if args.dated_glob:
        dated = sorted(source.glob(args.dated_glob))

    companies = Path(args.companies_csv) if args.companies_csv else None
    result = publisher.publish_from_directory(
        source_dir=source,
        ats_csv_pattern=args.pattern,
        dated_snapshots=dated,
        companies_csv=companies,
    )

    print(f"✓ Published {result.total_jobs:,} jobs from {result.total_companies:,} companies")
    print(f"  Manifest: {result.manifest_key}")
    print(f"  Files:    {len(result.files)}")
    print(f"  Duration: {result.duration_seconds:.1f}s")
    return 0


def _cmd_list_ats() -> int:
    from jobhive.client import _default_client

    manifest = _default_client().manifest
    for ats, entry in sorted(manifest.by_ats.items()):
        print(f"{ats.value:20s} {entry.rows:>10,} jobs")
    print(f"\nTotal: {manifest.stats.total_jobs:,} jobs across {manifest.stats.ats_count} ATSes")
    return 0


def _emit(df, fmt: str) -> None:
    if fmt == "csv":
        df.to_csv(sys.stdout, index=False)
    elif fmt == "json":
        df.to_json(sys.stdout, orient="records", indent=2)
        print()
    else:
        print(df.to_string(index=False))


if __name__ == "__main__":
    raise SystemExit(main())
