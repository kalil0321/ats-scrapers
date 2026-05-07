"""Continuous tenant-discovery daemon.

Runs an infinite loop: pick the least-recently-run query, walk
SearXNG result pages, verify each new slug against the live ATS API,
and write growing per-ATS CSVs to ``OUT_DIR``. State is persisted in
a SQLite DB (``STATE_DB``) so each cycle finds *new* tenants instead
of re-verifying the same ones.

Designed to run in Docker alongside SearXNG (see
``../searxng-jobhive/docker-compose.yml``). Survives container
restarts; just pick up where it left off via the state DB.

Knobs (env vars):

  SEARXNG_URL       SearXNG base URL.        (default http://localhost:8888)
  STATE_DB          SQLite path.             (default /var/lib/discovery/state.db)
  OUT_DIR           CSV output directory.    (default /var/lib/discovery/out)
  COMPANIES_CSV     Local cache of published companies/all.csv.
  CYCLE_SLEEP       Seconds between cycles. (default 300)
  PAGES_PER_QUERY   SearXNG pages to walk.   (default 10)
  QUERY_BATCH       Queries per cycle.       (default 20)
  REVERIFY_AFTER    Re-verify a known slug after N days. (default 30)

The query universe is generated programmatically as
``site:{host} {keyword}`` over a Cartesian product of cities × US
states × industries × roles per ATS. ~5,000+ unique queries per
ATS. The DB tracks ``last_run`` so we cycle through fairly.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

UTC = timezone.utc


def utcnow() -> datetime:
    """Python 3.13 deprecates ``datetime.utcnow()``. We still want
    naive-looking ISO strings in SQLite, so strip the tz after
    constructing the UTC instant — keeps existing rows string-
    compatible.
    """
    return datetime.now(UTC).replace(tzinfo=None)

# ``examples/`` is a flat scripts directory with no __init__.py, so
# sibling imports only work when this script's own dir is on
# sys.path. Add it ourselves so direct ``python …`` invocation works.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from discover_tenants import (  # noqa: E402  — sibling import after sys.path tweak
    ALL_ATS,
    AtsConfig,
    fetch_known,
)

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
STATE_DB = Path(os.environ.get("STATE_DB", "/var/lib/discovery/state.db"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/var/lib/discovery/out"))
COMPANIES_CSV = Path(os.environ.get("COMPANIES_CSV", "/var/lib/discovery/companies_all.csv"))
CYCLE_SLEEP = int(os.environ.get("CYCLE_SLEEP", "300"))
PAGES_PER_QUERY = int(os.environ.get("PAGES_PER_QUERY", "10"))
QUERY_BATCH = int(os.environ.get("QUERY_BATCH", "20"))
REVERIFY_AFTER = int(os.environ.get("REVERIFY_AFTER", "30"))  # days

log = logging.getLogger("discoverer")


# --- query universe ---------------------------------------------------------


def _site_keywords() -> list[str]:
    """The full keyword universe to combine with each ATS's ``site:``
    operator. ~70+ keywords; combined with cross-ATS hosts gives
    300+ queries per ATS, no duplicates.
    """
    cities = (
        "new york", "san francisco", "los angeles", "chicago", "boston",
        "austin", "seattle", "washington dc", "atlanta", "miami", "dallas",
        "houston", "phoenix", "denver", "portland", "minneapolis",
        "detroit", "philadelphia", "san diego", "raleigh", "salt lake city",
        "tampa", "orlando", "nashville", "columbus", "indianapolis",
        "kansas city", "saint louis", "pittsburgh", "cincinnati", "cleveland",
        "charlotte", "tucson", "albuquerque", "fresno", "sacramento",
        "milwaukee", "el paso", "boise", "spokane", "des moines",
        "london", "manchester", "edinburgh", "toronto", "vancouver",
        "montreal", "calgary", "sydney", "melbourne", "brisbane", "perth",
        "auckland", "wellington",
    )
    us_states = (
        "california", "texas", "new york state", "florida", "illinois",
        "pennsylvania", "ohio", "georgia", "north carolina", "michigan",
        "virginia", "washington state", "massachusetts", "arizona",
        "tennessee", "indiana", "missouri", "maryland", "wisconsin",
        "colorado", "minnesota", "south carolina", "alabama", "louisiana",
        "kentucky", "oregon", "oklahoma", "connecticut", "iowa", "utah",
        "nevada", "arkansas", "kansas", "mississippi", "nebraska",
    )
    industries = (
        "healthcare", "biotech", "fintech", "banking", "retail",
        "manufacturing", "energy", "automotive", "aerospace",
        "insurance", "logistics", "real estate", "education",
        "media", "telecommunications", "government",
        "ai", "cybersecurity", "saas", "consulting", "pharma",
        "ecommerce", "gaming", "cloud", "construction", "agriculture",
        "hospitality", "travel", "food", "beverage", "fashion",
        "sports", "fitness", "music", "art", "law", "accounting",
    )
    roles = (
        "engineer", "manager", "director", "vp", "intern",
        "analyst", "designer", "scientist", "nurse", "physician",
        "attorney", "consultant", "founder", "architect", "researcher",
        "technician", "specialist", "lead", "head",
    )
    extras = (
        "remote", "hybrid", "onsite", "full time", "part time",
        "contract", "internship", "entry level", "senior", "junior",
        "principal", "staff",
    )
    return list(cities) + list(us_states) + list(industries) + list(roles) + list(extras)


def queries_for_ats(ats: AtsConfig) -> list[str]:
    """Generate every query for an ATS by combining its base ``site:``
    operators with each keyword. Returns a stable, ordered list.
    """
    keywords = [""] + _site_keywords()
    # Pull the host(s) the existing AtsConfig knows about by inspecting
    # its baked queries. They all start with ``site:HOST`` so we can
    # extract the hosts without duplicating the data.
    hosts: list[str] = []
    for q in ats.queries:
        m = re.match(r"site:([\w.-]+)", q)
        if m and m.group(1) not in hosts:
            hosts.append(m.group(1))
    out: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        for kw in keywords:
            q = f"site:{host}" + (f" {kw}" if kw else "")
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out


# --- state DB ---------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    ats         TEXT NOT NULL,
    query       TEXT NOT NULL,
    last_run    TEXT,           -- ISO-8601 datetime
    runs        INTEGER NOT NULL DEFAULT 0,
    last_new    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ats, query)
);

CREATE TABLE IF NOT EXISTS tenants (
    ats           TEXT NOT NULL,
    slug          TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_verified TEXT,
    verified      INTEGER,        -- 0 = no, 1 = yes
    last_status   TEXT,            -- e.g. http_200, http_404
    PRIMARY KEY (ats, slug)
);

CREATE INDEX IF NOT EXISTS ix_queries_lastrun ON queries (ats, last_run);
CREATE INDEX IF NOT EXISTS ix_tenants_verified ON tenants (ats, verified);
"""


@contextmanager
def db_conn(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def seed_queries(conn: sqlite3.Connection, ats: AtsConfig) -> None:
    """Insert every query for an ATS into the queries table. Idempotent
    via the (ats, query) primary key."""
    rows = [(ats.name, q) for q in queries_for_ats(ats)]
    conn.executemany(
        "INSERT OR IGNORE INTO queries (ats, query) VALUES (?, ?)",
        rows,
    )
    log.info("seeded %d queries for %s (existing rows untouched)", len(rows), ats.name)


def pick_queries(
    conn: sqlite3.Connection,
    ats_name: str,
    n: int,
) -> list[str]:
    """Return the N least-recently-run queries for the ATS. NULL
    last_run sorts first so unrun queries get priority."""
    rows = conn.execute(
        """
        SELECT query FROM queries
        WHERE ats = ?
        ORDER BY (last_run IS NULL) DESC, last_run ASC
        LIMIT ?
        """,
        (ats_name, n),
    ).fetchall()
    return [r["query"] for r in rows]


def mark_query_run(
    conn: sqlite3.Connection,
    ats_name: str,
    query: str,
    new_count: int,
) -> None:
    conn.execute(
        """
        UPDATE queries
        SET last_run = ?, runs = runs + 1, last_new = ?
        WHERE ats = ? AND query = ?
        """,
        (utcnow().isoformat(), new_count, ats_name, query),
    )


def known_slugs(conn: sqlite3.Connection, ats_name: str) -> set[str]:
    rows = conn.execute(
        "SELECT slug FROM tenants WHERE ats = ?", (ats_name,),
    ).fetchall()
    return {r["slug"] for r in rows}


def verified_slugs(conn: sqlite3.Connection, ats_name: str) -> set[str]:
    rows = conn.execute(
        "SELECT slug FROM tenants WHERE ats = ? AND verified = 1",
        (ats_name,),
    ).fetchall()
    return {r["slug"] for r in rows}


def upsert_tenant(
    conn: sqlite3.Connection,
    ats_name: str,
    slug: str,
    *,
    verified: bool | None,
    status: str | None,
) -> None:
    now = utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO tenants (ats, slug, first_seen, last_verified, verified, last_status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (ats, slug) DO UPDATE SET
            last_verified = excluded.last_verified,
            verified      = excluded.verified,
            last_status   = excluded.last_status
        """,
        (
            ats_name, slug, now,
            now if verified is not None else None,
            1 if verified else (0 if verified is False else None),
            status,
        ),
    )


# --- search + verify --------------------------------------------------------


class SearchSuspended(Exception):
    """SearXNG returned an empty result list with one or more
    upstream engines reporting suspended state. The query was not
    actually answered — caller should NOT mark it as 'run' so it
    gets retried in a future cycle."""


def searxng_search(
    client: httpx.Client,
    query: str,
    pages: int,
    page_delay: float = 0.5,
) -> Iterable[str]:
    """Yield every result URL across N pages of one SearXNG query.

    Raises ``SearchSuspended`` if page 1 comes back empty AND
    SearXNG reports any unresponsive upstream engines — the most
    common reason for empty results. Caller should treat this as
    a soft-failure and re-queue the query.
    """
    yielded = 0
    for page in range(1, pages + 1):
        try:
            r = client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json", "pageno": page},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            log.warning("SearXNG transport error %r — sleeping 10s", exc)
            time.sleep(10)
            return
        if r.status_code != 200:
            log.warning("SearXNG returned %d for %r page=%d", r.status_code, query, page)
            time.sleep(5)
            return
        try:
            payload = r.json()
        except ValueError:
            return
        results = payload.get("results") or []
        if not results:
            # Page 1 came back empty: distinguish "Google indexed
            # nothing" (a real zero) from "all engines are
            # CAPTCHA'd" (a soft failure). The latter shows up in
            # ``unresponsive_engines``.
            if page == 1 and yielded == 0:
                unresponsive = payload.get("unresponsive_engines") or []
                if unresponsive:
                    raise SearchSuspended(
                        f"empty results + suspended engines: {unresponsive}"
                    )
            return
        for res in results:
            url = res.get("url") or ""
            if url:
                yielded += 1
                yield url
        time.sleep(page_delay)


def verify_one(ats: AtsConfig, slug: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    url = ats.verify_url(slug)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if ats.name == "workday":
                r = client.post(
                    url,
                    json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                )
                ok = (
                    r.status_code == 200
                    and isinstance(r.json(), dict)
                    and "jobPostings" in r.json()
                )
                return ok, f"http_{r.status_code}"
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            return ats.verify_ok(r), f"http_{r.status_code}"
    except Exception as exc:
        return False, type(exc).__name__


# --- output -----------------------------------------------------------------


def write_snapshot(
    conn: sqlite3.Connection,
    ats_name: str,
    out_dir: Path,
    published_known: set[str],
) -> int:
    """Write two files per ATS, both in ``companies/all.csv`` schema:

    - ``{ats}.csv``: every verified slug for this ATS — published +
      newly discovered. Suitable for direct sync into the publisher's
      canonical list.
    - ``new_{ats}.csv``: delta only — slugs NOT yet in the published
      ``companies/all.csv``. Useful for review / merge requests.

    Returns the count of net-new slugs (the delta).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    verified = verified_slugs(conn, ats_name)
    new_verified = verified - published_known
    full = verified | published_known  # union: every known-good slug

    full_path = out_dir / f"{ats_name}.csv"
    with full_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "ats"])
        for slug in sorted(full):
            w.writerow([slug, ats_name])

    delta_path = out_dir / f"new_{ats_name}.csv"
    with delta_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "ats"])
        for slug in sorted(new_verified):
            w.writerow([slug, ats_name])

    return len(new_verified)


# --- main loop --------------------------------------------------------------


def cycle(
    conn: sqlite3.Connection,
    http: httpx.Client,
    published_known: dict[str, set[str]],
) -> dict[str, dict[str, int]]:
    """One pass: pick the next batch of queries per ATS, walk
    SearXNG pages round-robin across ATSes (so a single ATS's burst
    of queries doesn't exhaust the upstream-engine rate-limit budget
    before the others get a turn), verify new slugs, persist.
    Returns per-ATS counters for logging.
    """
    # Per-ATS bookkeeping the inner loop reads/writes.
    plan: dict[str, dict] = {}
    for ats_name, ats in ALL_ATS.items():
        plan[ats_name] = {
            "ats": ats,
            "seen_in_db": known_slugs(conn, ats_name),
            "before_verified": len(verified_slugs(conn, ats_name)),
            "queries": pick_queries(conn, ats_name, QUERY_BATCH),
            "new_seen": 0,
            "new_verified": 0,
        }

    # Round-robin: take the i-th query from each ATS in turn until
    # every list is exhausted. Spreads upstream-engine load evenly.
    max_len = max((len(p["queries"]) for p in plan.values()), default=0)
    aborted = False
    for i in range(max_len):
        if aborted:
            break
        for ats_name, p in plan.items():
            if i >= len(p["queries"]):
                continue
            ats = p["ats"]
            q = p["queries"][i]
            new_count = 0
            try:
                for url in searxng_search(http, q, PAGES_PER_QUERY):
                    m = ats.url_regex.search(url)
                    if not m:
                        continue
                    slug = ats.extract_slug(m)
                    if not slug:
                        continue
                    if slug in p["seen_in_db"]:
                        continue
                    p["seen_in_db"].add(slug)
                    p["new_seen"] += 1
                    new_count += 1
                    ok, status = verify_one(ats, slug)
                    upsert_tenant(conn, ats_name, slug, verified=ok, status=status)
                    if ok:
                        p["new_verified"] += 1
                mark_query_run(conn, ats_name, q, new_count)
            except SearchSuspended as exc:
                # Leave last_run NULL so this query gets re-picked
                # next cycle. Abort the rest of the round-robin —
                # once one engine pool is suspended, the others are
                # likely to be too, and there's no point burning
                # through more queries.
                log.warning(
                    "engines suspended on %s/%s — aborting cycle: %s",
                    ats_name, q, exc,
                )
                aborted = True
                break
            # Inter-query cool-down: with ~150 queries/cycle the
            # upstream engines suspend without a brief pause. 1s is
            # enough to keep Google/Bing happy across long runs.
            time.sleep(1.0)

    stats: dict[str, dict[str, int]] = {}
    for ats_name, p in plan.items():
        snapshot_count = write_snapshot(
            conn, ats_name, OUT_DIR, published_known.get(ats_name, set())
        )
        stats[ats_name] = {
            "before_verified": p["before_verified"],
            "after_verified": p["before_verified"] + p["new_verified"],
            "newly_seen": p["new_seen"],
            "newly_verified": p["new_verified"],
            "snapshot_csv_rows": snapshot_count,
            "queries_run": len(p["queries"]),
        }
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("starting discoverer")
    log.info(
        "config: SEARXNG_URL=%s STATE_DB=%s OUT_DIR=%s "
        "CYCLE_SLEEP=%ds PAGES_PER_QUERY=%d QUERY_BATCH=%d",
        SEARXNG_URL, STATE_DB, OUT_DIR, CYCLE_SLEEP, PAGES_PER_QUERY, QUERY_BATCH,
    )

    with db_conn(STATE_DB) as conn:
        # Seed the query universe once per ATS (idempotent).
        for ats in ALL_ATS.values():
            seed_queries(conn, ats)

        # Load the published companies CSV once per cycle below; cache
        # for ~6h since it changes daily at most.
        published_known: dict[str, set[str]] = {}
        last_known_refresh = utcnow() - timedelta(hours=24)

        while True:
            if utcnow() - last_known_refresh > timedelta(hours=6):
                try:
                    log.info("refreshing published companies CSV …")
                    published_known = fetch_known(SEARXNG_URL, COMPANIES_CSV)
                    last_known_refresh = utcnow()
                    for ats_name, slugs in published_known.items():
                        log.info("  published %s: %d slugs", ats_name, len(slugs))
                except Exception as exc:
                    log.warning("failed to refresh known: %s", exc)

            with httpx.Client(timeout=30) as http:
                try:
                    stats = cycle(conn, http, published_known)
                except Exception as exc:
                    log.exception("cycle failed: %s", exc)
                    time.sleep(60)
                    continue

            for ats_name, s in stats.items():
                log.info(
                    "  %s: queries=%d new_seen=%d new_verified=%d "
                    "verified_total=%d snapshot_csv_rows=%d",
                    ats_name, s["queries_run"], s["newly_seen"],
                    s["newly_verified"], s["after_verified"],
                    s["snapshot_csv_rows"],
                )

            log.info("cycle done; sleeping %ds", CYCLE_SLEEP)
            time.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    sys.exit(main())
