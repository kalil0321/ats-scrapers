"""Discover new ATS tenant slugs via local SearXNG, compute the
delta against the published ``companies/all.csv``, and write the
deltas to per-ATS CSV files.

The big multi-tenant ATSes (Workday, Greenhouse, Lever, Ashby) host
thousands of US enterprise tenants — way more than the publisher's
current company list reaches. Each new slug we discover unlocks
hundreds of jobs that the corresponding scraper can already pull.

Usage:

    # Spin up SearXNG (one-time, in ../searxng-jobhive)
    cd ../searxng-jobhive && docker compose up -d

    # Run discovery
    python examples/discover_tenants.py [--ats workday] [--ats greenhouse]
        [--searxng-url http://localhost:8888]
        [--queries-per-ats 7]
        [--pages-per-query 10]
        [--out-dir /tmp/discovery]

Output: ``new_{ats}.csv`` files with one slug per line, listing only
slugs NOT yet in the published ``companies/all.csv``. Feed these
into the publisher's per-ATS scrape loop.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd

DEFAULT_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
COMPANIES_CSV_URL = "https://storage.stapply.ai/jobhive/v1/companies/all.csv"


@dataclass
class AtsConfig:
    name: str
    queries: tuple[str, ...]
    url_regex: re.Pattern
    extract_slug: callable  # match → slug
    verify_url: callable    # slug → verification URL (hit it; expect 200)
    verify_ok: callable = lambda r: r.status_code == 200  # noqa: E731


# Workday: tenant subdomain is the first label of {co}.wd{N}.myworkdayjobs.com.
# We need to keep the (slug, instance, site) triple for verification — the
# slug alone isn't a full Workday tenant address. Each query result gives
# us the triple; we cache it and verify by hitting the cxs API.
_WORKDAY_TRIPLE_CACHE: dict[str, tuple[str, str, str]] = {}


def _workday_extract(m: re.Match) -> str:
    slug = m.group(1).lower()
    # Stash the (instance, site) discovered alongside this slug — needed
    # for the verify step. First-seen wins; tenants don't change instance.
    _WORKDAY_TRIPLE_CACHE.setdefault(slug, (slug, m.group(2), m.group(3)))
    return slug


def _workday_verify_url(slug: str) -> str:
    triple = _WORKDAY_TRIPLE_CACHE.get(slug)
    if not triple:
        return f"https://{slug}.wd1.myworkdayjobs.com/wday/cxs/{slug}/CareersExternal/jobs"
    co, instance, site = triple
    return f"https://{co}.{instance}.myworkdayjobs.com/wday/cxs/{co}/{site}/jobs"


WORKDAY = AtsConfig(
    name="workday",
    queries=(
        "site:myworkdayjobs.com",
        "site:myworkdayjobs.com careers",
        "site:myworkdayjobs.com engineer",
        "site:myworkdayjobs.com senior",
        "site:myworkdayjobs.com manager",
        "site:myworkdayjobs.com analyst",
        "site:myworkdayjobs.com director",
        "site:myworkdayjobs.com intern",
        "site:myworkdayjobs.com nurse",
        "site:myworkdayjobs.com sales",
        "site:myworkdayjobs.com marketing",
        "site:myworkdayjobs.com finance",
        "site:myworkdayjobs.com healthcare",
        "site:myworkdayjobs.com retail",
    ),
    url_regex=re.compile(
        r"https?://([^./]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)"
    ),
    extract_slug=_workday_extract,
    verify_url=_workday_verify_url,
    # Workday's jobs endpoint is POST not GET; verify_ok must POST.
    # We swap to a POST in the verify loop based on this sentinel.
)

# Greenhouse has TWO host shapes: boards.greenhouse.io and
# job-boards.greenhouse.io (the new layout). Both use the same slug
# in the first path segment.
GREENHOUSE = AtsConfig(
    name="greenhouse",
    queries=(
        "site:boards.greenhouse.io",
        "site:job-boards.greenhouse.io",
        "site:boards.greenhouse.io engineer",
        "site:job-boards.greenhouse.io engineer",
        "site:boards.greenhouse.io manager",
        "site:job-boards.greenhouse.io manager",
        "site:boards.greenhouse.io senior",
        "site:job-boards.greenhouse.io senior",
        "site:boards.greenhouse.io intern",
        "site:job-boards.greenhouse.io marketing",
    ),
    url_regex=re.compile(
        r"https?://(?:job-)?boards\.greenhouse\.io/([a-zA-Z0-9_-]+)"
    ),
    extract_slug=lambda m: m.group(1).lower(),
    # Greenhouse public jobs API: GET returns {"jobs": [...]}.
    # 404 / 410 → board doesn't exist or is closed.
    verify_url=lambda slug: f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    verify_ok=lambda r: r.status_code == 200 and len(((r.json() or {}).get("jobs") or [])) >= 0,
)

LEVER = AtsConfig(
    name="lever",
    queries=(
        "site:jobs.lever.co",
        "site:jobs.lever.co engineer",
        "site:jobs.lever.co manager",
        "site:jobs.lever.co senior",
        "site:jobs.lever.co intern",
        "site:jobs.lever.co marketing",
        "site:jobs.lever.co operations",
    ),
    url_regex=re.compile(r"https?://jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
    extract_slug=lambda m: m.group(1),  # Lever slugs ARE case-sensitive
    # Lever public API: GET returns a JSON array of postings.
    verify_url=lambda slug: f"https://api.lever.co/v0/postings/{slug}?mode=json",
    verify_ok=lambda r: r.status_code == 200 and isinstance(r.json(), list),
)

ASHBY = AtsConfig(
    name="ashby",
    queries=(
        "site:jobs.ashbyhq.com",
        "site:jobs.ashbyhq.com engineer",
        "site:jobs.ashbyhq.com senior",
        "site:jobs.ashbyhq.com founding",
        "site:jobs.ashbyhq.com manager",
        "site:jobs.ashbyhq.com intern",
        "site:jobs.ashbyhq.com designer",
    ),
    url_regex=re.compile(r"https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
    extract_slug=lambda m: m.group(1).lower(),
    # Ashby's public job-board API returns the board metadata + jobs.
    verify_url=lambda slug: f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
    verify_ok=lambda r: (
        r.status_code == 200 and isinstance(r.json(), dict) and "jobs" in r.json()
    ),
)

ALL_ATS: dict[str, AtsConfig] = {
    "workday": WORKDAY,
    "greenhouse": GREENHOUSE,
    "lever": LEVER,
    "ashby": ASHBY,
}


def known_slugs_per_ats(csv_path: Path) -> dict[str, set[str]]:
    """Read ``companies/all.csv`` and return ``{ats: {lowercased_slug, …}}``."""
    df = pd.read_csv(csv_path)
    out: dict[str, set[str]] = {}
    for ats, sub in df.groupby("ats"):
        out[str(ats)] = set(sub["slug"].astype(str).str.lower())
    return out


def fetch_known(searxng_url: str, csv_path: Path) -> dict[str, set[str]]:
    """Download the published companies CSV (cached locally) and parse it."""
    if not csv_path.exists():
        print(f"  fetching {COMPANIES_CSV_URL} → {csv_path} …", file=sys.stderr)
        with httpx.Client(timeout=120) as client:
            r = client.get(
                COMPANIES_CSV_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"},
                follow_redirects=True,
            )
            r.raise_for_status()
            csv_path.write_bytes(r.content)
    return known_slugs_per_ats(csv_path)


def discover_one_ats(
    ats: AtsConfig,
    *,
    searxng_url: str,
    pages_per_query: int,
    request_delay: float = 0.5,
) -> set[str]:
    """Run all queries for one ATS, return the union of slugs found."""
    slugs: set[str] = set()
    with httpx.Client(timeout=30) as client:
        for q in ats.queries:
            new_at_query = 0
            for page in range(1, pages_per_query + 1):
                try:
                    r = client.get(
                        f"{searxng_url}/search",
                        params={"q": q, "format": "json", "pageno": page},
                    )
                except httpx.HTTPError as exc:
                    print(f"  WARN  {ats.name}: {q!r} page={page} → {exc}", file=sys.stderr)
                    break
                if r.status_code != 200:
                    print(
                        f"  WARN  {ats.name}: {q!r} page={page} → "
                        f"{r.status_code}",
                        file=sys.stderr,
                    )
                    break
                results = r.json().get("results") or []
                if not results:
                    break
                for res in results:
                    m = ats.url_regex.search(res.get("url", "") or "")
                    if not m:
                        continue
                    slug = ats.extract_slug(m)
                    if slug and slug not in slugs:
                        slugs.add(slug)
                        new_at_query += 1
                time.sleep(request_delay)
            print(
                f"    {ats.name:11s} {q[:55]:55s} → {len(slugs)} cumulative",
                file=sys.stderr,
            )
    return slugs


def write_csv(path: Path, slugs: set[str], ats_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "ats"])
        for s in sorted(slugs):
            w.writerow([s, ats_name])


def verify_slugs(
    ats: AtsConfig,
    slugs: set[str],
    *,
    timeout: float = 15.0,
    concurrency: int = 8,
) -> tuple[set[str], dict[str, str]]:
    """Hit each ATS's public jobs API for every slug. Returns
    ``(working_slugs, dropped_with_reason)`` so the caller can log
    why each rejection happened (404 dead board, 403 closed,
    timeout, etc.).
    """
    import concurrent.futures
    working: set[str] = set()
    dropped: dict[str, str] = {}

    def _check(slug: str) -> tuple[str, bool, str]:
        url = ats.verify_url(slug)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                if ats.name == "workday":
                    # Workday's jobs endpoint is POST with empty body.
                    r = client.post(
                        url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                    )
                    ok = (
                        r.status_code == 200
                        and isinstance(r.json(), dict)
                        and "jobPostings" in r.json()
                    )
                    return (slug, ok, f"http_{r.status_code}")
                r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                ok = ats.verify_ok(r)
                return (slug, ok, f"http_{r.status_code}")
        except Exception as exc:
            return (slug, False, type(exc).__name__)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_check, s) for s in sorted(slugs)]
        for fut in concurrent.futures.as_completed(futures):
            slug, ok, reason = fut.result()
            if ok:
                working.add(slug)
            else:
                dropped[slug] = reason
    return working, dropped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover new ATS tenant slugs via local SearXNG.",
    )
    parser.add_argument(
        "--ats", action="append", choices=sorted(ALL_ATS),
        help="ATS to discover (repeatable). Default: all.",
    )
    parser.add_argument(
        "--searxng-url", default=DEFAULT_SEARXNG_URL,
        help=f"SearXNG base URL. Default: {DEFAULT_SEARXNG_URL}",
    )
    parser.add_argument(
        "--pages-per-query", type=int, default=10,
        help="Max pages to walk per query. Default: 10.",
    )
    parser.add_argument(
        "--out-dir", default="/tmp/discovery",
        help="Output directory for new_{ats}.csv. Default: /tmp/discovery.",
    )
    parser.add_argument(
        "--companies-csv", default="/tmp/companies_all.csv",
        help="Local cache path for the published companies CSV.",
    )
    parser.add_argument(
        "--no-verify", action="store_true",
        help="Skip the post-discovery verification pass (faster but ships "
             "broken slugs that the publisher would error on).",
    )
    args = parser.parse_args()

    targets = [ALL_ATS[a] for a in (args.ats or sorted(ALL_ATS))]
    out_dir = Path(args.out_dir)

    print(f"  loading known slugs from {args.companies_csv} …", file=sys.stderr)
    known = fetch_known(args.searxng_url, Path(args.companies_csv))
    for ats_name, slugs in sorted(known.items()):
        print(f"    known {ats_name:15s}: {len(slugs):>6,} slugs", file=sys.stderr)

    summary: list[tuple[str, int, int, int, int]] = []
    for ats in targets:
        print(f"\n=== {ats.name} ===", file=sys.stderr)
        discovered = discover_one_ats(
            ats,
            searxng_url=args.searxng_url,
            pages_per_query=args.pages_per_query,
        )
        known_for_ats = known.get(ats.name, set())
        new_slugs = discovered - known_for_ats
        verified = new_slugs
        if not args.no_verify and new_slugs:
            print(
                f"  verifying {len(new_slugs)} new {ats.name} slugs against "
                "the live ATS API …",
                file=sys.stderr,
            )
            verified, dropped = verify_slugs(ats, new_slugs)
            # Group drop reasons for a one-line summary.
            from collections import Counter
            reason_counts = Counter(dropped.values())
            if reason_counts:
                breakdown = ", ".join(
                    f"{r}: {c}" for r, c in reason_counts.most_common(5)
                )
                print(
                    f"  dropped {len(dropped)}/{len(new_slugs)} non-working "
                    f"slugs ({breakdown})",
                    file=sys.stderr,
                )
        out_path = out_dir / f"new_{ats.name}.csv"
        write_csv(out_path, verified, ats.name)
        print(
            f"  {ats.name}: discovered={len(discovered)}, "
            f"already-known={len(discovered & known_for_ats)}, "
            f"NEW={len(new_slugs)}, "
            f"VERIFIED={len(verified)} → {out_path}",
            file=sys.stderr,
        )
        summary.append((
            ats.name, len(discovered), len(discovered & known_for_ats),
            len(new_slugs), len(verified),
        ))

    print("\n=== Summary ===")
    print(f"{'ATS':12s} {'Discovered':>11s} {'Known':>8s} {'NEW':>8s} {'VERIFIED':>10s}")
    for name, d, k, n, v in summary:
        print(f"{name:12s} {d:>11,} {k:>8,} {n:>8,} {v:>10,}")
    print(f"Outputs in: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
