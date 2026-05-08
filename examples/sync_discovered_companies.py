"""Sync verified tenants from the discoverer's SQLite state into
the per-provider ``{ats}/{ats}_companies.csv`` files.

The discoverer (running in the searxng-jobhive Docker container)
maintains a SQLite DB of every tenant slug it has probed. This
script:

  1. Reads every existing ``{ats}/{ats}_companies.csv`` to learn
     the slugs already on the publisher's list.
  2. Queries the discoverer's SQLite for every ``verified=1`` slug
     per ATS.
  3. Computes the delta and appends new rows in the existing
     ``name,url`` schema, with the URL constructed from the
     per-ATS pattern.

Workday is special-cased: each tenant's URL needs ``(slug,
instance, site)``. We hit ``robots.txt`` per slug to recover the
canonical instance + site (same trick the brute-force probe
uses), then build the public careers URL.

Usage:

    python scripts/sync_discovered_companies.py [--dry-run]
        [--state-db /path/to/state.db]   # default: pulls from container
        [--ats name1,name2]              # default: all 12 known ATSes

The default ``--state-db`` reads from the running
``discoverer-jobhive`` container via ``docker cp``.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("sync")


# --- per-ATS URL builders --------------------------------------------------
#
# Each callable takes a discovered slug and returns ``(name, url)``
# in the schema used by ``{ats}_companies.csv`` (``name`` = display,
# ``url`` = public careers URL). Workday uses a separate path that
# resolves ``(instance, site)`` first, so its builder isn't used.

UrlBuilder = Callable[[str], tuple[str, str]]


def _passthrough_name(slug: str) -> str:
    """Use the slug itself as the display name. The publisher can
    pretty-print later — the existing CSVs already mix slug-style
    and human-readable names, so consistency isn't strict."""
    return slug


URL_BUILDERS: dict[str, UrlBuilder] = {
    "greenhouse": lambda s: (
        _passthrough_name(s),
        f"https://job-boards.greenhouse.io/{s}",
    ),
    "lever":     lambda s: (s, f"https://jobs.lever.co/{s}"),
    "ashby":     lambda s: (s, f"https://jobs.ashbyhq.com/{s}"),
    "bamboohr":  lambda s: (s, f"https://{s}.bamboohr.com/careers"),
    "breezy":    lambda s: (s, s),
    "jazzhr":    lambda s: (s, f"https://{s}.applytojob.com"),
    "personio":  lambda s: (s, f"https://{s}.jobs.personio.com"),
    "recruitee": lambda s: (s, f"https://{s}.recruitee.com"),
    "teamtailor": lambda s: (s, f"https://{s}.teamtailor.com"),
    "workable":  lambda s: (s, f"https://apply.workable.com/{s}"),
    "rippling":  lambda s: (s, f"https://ats.rippling.com/{s}/jobs"),
}

# Workday gets resolved via robots.txt — see resolve_workday().
_WORKDAY_INSTANCES = ("wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd103")
_WORKDAY_SITEMAP_RE = re.compile(
    r"Sitemap:\s*https?://[\w-]+\.(wd\d+)\.myworkdayjobs\.com/([\w-]+)/siteMap\.xml",
    re.IGNORECASE,
)


def resolve_workday(slug: str, *, timeout: float = 5.0) -> tuple[str, str] | None:
    """Returns ``(name, url)`` if the slug resolves on any wdN
    instance, otherwise None. Walks robots.txt across the candidate
    instances; the Sitemap line tells us the canonical site name."""
    for wd in _WORKDAY_INSTANCES:
        url = f"https://{slug}.{wd}.myworkdayjobs.com/robots.txt"
        try:
            r = httpx.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        m = _WORKDAY_SITEMAP_RE.search(r.text)
        if m:
            instance, site = m.group(1), m.group(2)
            return (slug, f"https://{slug}.{instance}.myworkdayjobs.com/{site}")
    return None


# --- existing-CSV inspection ----------------------------------------------


def _normalize_slug(s: str) -> str:
    """Lowercase + strip non-slug chars. Used for de-duplication
    against existing CSVs whose 'name' column is sometimes a
    display name, sometimes a slug."""
    return "".join(c for c in s.lower() if c.isalnum() or c in "-_")


def _slugs_from_url(ats: str, url: str) -> str | None:
    """Extract the slug portion of a public careers URL — used to
    dedupe new discoveries against tenants already in the publisher's
    CSV under any of the per-ATS URL shapes."""
    if not url:
        return None
    patterns = {
        "workday":   r"https?://([^./]+)\.wd\d+\.myworkdayjobs\.com",
        "greenhouse": r"https?://(?:job-)?boards(?:-api)?\.greenhouse\.io/([a-zA-Z0-9_-]+)",
        "lever":     r"https?://jobs\.lever\.co/([a-zA-Z0-9_-]+)",
        "ashby":     r"https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)",
        "bamboohr":  r"https?://([a-zA-Z0-9-]+)\.bamboohr\.com",
        "breezy":    r"https?://([a-zA-Z0-9-]+)\.breezy\.hr",
        "jazzhr":    r"https?://([a-zA-Z0-9-]+)\.applytojob\.com",
        "personio":  r"https?://([a-zA-Z0-9-]+)\.jobs\.personio\.com",
        "recruitee": r"https?://([a-zA-Z0-9-]+)\.recruitee\.com",
        "teamtailor": r"https?://([a-zA-Z0-9-]+)\.teamtailor\.com",
        "workable":  r"https?://apply\.workable\.com/([a-zA-Z0-9_-]+)",
        "rippling":  r"https?://ats\.rippling\.com/([a-zA-Z0-9_-]+)",
    }
    pat = patterns.get(ats)
    if not pat:
        return None
    m = re.search(pat, url)
    return _normalize_slug(m.group(1)) if m else None


def existing_slugs(ats: str, csv_path: Path) -> set[str]:
    """Read the existing per-provider CSV and return the set of
    normalized slugs already represented (under any URL shape)."""
    if not csv_path.exists():
        return set()
    out: set[str] = set()
    with csv_path.open(newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            slug_from_url = _slugs_from_url(ats, row.get("url", "") or "")
            if slug_from_url:
                out.add(slug_from_url)
            else:
                # Fall back to the name column when URL doesn't parse
                # (e.g., breezy where the URL is just the slug).
                name = (row.get("name") or "").strip()
                if name:
                    out.add(_normalize_slug(name))
    return out


# --- state-db pull ---------------------------------------------------------


def pull_state_db_from_container(container: str, dest: Path) -> Path:
    """Copy the live state.db out of the discoverer container into
    a local temp file. Cheaper than installing sqlite-over-the-wire."""
    src = f"{container}:/var/lib/discovery/state.db"
    log.info("pulling SQLite from %s -> %s", src, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "cp", src, str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def verified_slugs_from_db(db_path: Path, ats: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT slug FROM tenants WHERE ats = ? AND verified = 1", (ats,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


# --- main sync -------------------------------------------------------------


def append_new_rows(
    csv_path: Path,
    rows: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> None:
    """Append ``(name, url)`` rows to the CSV, preserving the existing
    file order. Atomic write: copy → append → move."""
    if not rows:
        return
    if dry_run:
        for r in rows[:5]:
            log.info("    DRY: + %s,%s", *r)
        if len(rows) > 5:
            log.info("    DRY: + … (%d more)", len(rows) - 5)
        return
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    shutil.copy(csv_path, tmp)
    with tmp.open("a", newline="") as f:
        w = csv.writer(f)
        for name, url in rows:
            w.writerow([name, url])
    tmp.replace(csv_path)


def sync_ats(
    ats: str,
    db_path: Path,
    *,
    dry_run: bool,
    workday_concurrency: int = 16,
) -> tuple[int, int]:
    """Returns ``(checked_count, appended_count)``."""
    folder = REPO_ROOT / ats
    csv_path = folder / f"{ats}_companies.csv"
    if not csv_path.exists():
        log.warning("  %s: %s missing — skipped", ats, csv_path)
        return (0, 0)

    existing = existing_slugs(ats, csv_path)
    discovered = verified_slugs_from_db(db_path, ats)
    new = sorted(s for s in discovered if _normalize_slug(s) not in existing)
    log.info(
        "  %s: existing=%d, verified=%d, candidate-new=%d",
        ats, len(existing), len(discovered), len(new),
    )
    if not new:
        return (len(existing), 0)

    rows: list[tuple[str, str]] = []
    if ats == "workday":
        # Resolve each new slug to (instance, site) via robots.txt.
        # Skip slugs that don't resolve — the publisher would error
        # otherwise.
        with ThreadPoolExecutor(max_workers=workday_concurrency) as pool:
            futures = {pool.submit(resolve_workday, s): s for s in new}
            for fut in as_completed(futures):
                slug = futures[fut]
                result = fut.result()
                if result:
                    rows.append(result)
        log.info("    workday: %d/%d resolved via robots.txt", len(rows), len(new))
    else:
        builder = URL_BUILDERS.get(ats)
        if not builder:
            log.warning("  %s: no URL builder — skipped", ats)
            return (len(existing), 0)
        rows = [builder(s) for s in new]

    append_new_rows(csv_path, rows, dry_run=dry_run)
    return (len(existing), len(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="Path to state.db. Default: pull from discoverer-jobhive container.",
    )
    parser.add_argument(
        "--container", default="discoverer-jobhive",
        help="Container name to pull state.db from (only used when --state-db is omitted).",
    )
    parser.add_argument(
        "--ats", default="all",
        help="Comma-separated ATS names, or 'all' (default).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be appended without writing.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.ats == "all":
        ats_names = list(URL_BUILDERS) + ["workday"]
    else:
        ats_names = [a.strip() for a in args.ats.split(",")]

    if args.state_db:
        db_path = args.state_db
    else:
        db_path = REPO_ROOT / ".cache" / "state.db"
        pull_state_db_from_container(args.container, db_path)

    log.info("syncing %d ATSes from %s (dry_run=%s)", len(ats_names), db_path, args.dry_run)
    grand_appended = 0
    for ats in ats_names:
        existing, appended = sync_ats(ats, db_path, dry_run=args.dry_run)
        grand_appended += appended
    log.info(
        "DONE — %d new rows %s across %d ATSes",
        grand_appended,
        "would-be appended (dry-run)" if args.dry_run else "appended",
        len(ats_names),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
