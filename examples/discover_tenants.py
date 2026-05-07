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


def _site_query_set(site: str) -> tuple[str, ...]:
    """Generate a wide query set for a given site:X.com host.

    Google's ``site:`` operator returns the most-indexed pages first;
    adding generic role keywords ('engineer', 'manager') overlaps
    heavily across queries. The biggest variety wins come from
    *specific* terms — cities, industries, US states, niche stacks
    — that re-rank the result list to surface long-tail tenants.
    """
    cities = (
        "new york", "san francisco", "los angeles", "chicago", "boston",
        "austin", "seattle", "washington dc", "atlanta", "miami", "dallas",
        "houston", "phoenix", "denver", "portland", "minneapolis",
        "detroit", "philadelphia", "san diego", "raleigh", "salt lake city",
        "tampa", "orlando", "nashville", "columbus", "indianapolis",
        "kansas city", "saint louis", "pittsburgh", "cincinnati",
        "london", "toronto", "vancouver", "sydney", "melbourne",
    )
    us_states = (
        "california", "texas", "new york state", "florida", "illinois",
        "pennsylvania", "ohio", "georgia", "north carolina", "michigan",
        "virginia", "washington state", "massachusetts", "arizona",
        "tennessee", "indiana", "missouri", "maryland", "wisconsin",
        "colorado", "minnesota", "south carolina", "alabama", "louisiana",
        "kentucky", "oregon", "oklahoma", "connecticut",
    )
    industries = (
        "healthcare", "biotech", "fintech", "banking", "retail",
        "manufacturing", "energy", "automotive", "aerospace",
        "insurance", "logistics", "real estate", "education",
        "media", "telecommunications", "government",
        "ai", "cybersecurity", "saas", "consulting", "pharma",
        "ecommerce", "gaming", "cloud", "construction", "agriculture",
    )
    roles = (
        "engineer", "manager", "director", "vp", "intern",
        "analyst", "designer", "scientist", "nurse", "physician",
        "attorney", "consultant", "founder",
    )
    base = [f"site:{site}"]
    base += [f"site:{site} {city}" for city in cities]
    base += [f"site:{site} {state}" for state in us_states]
    base += [f"site:{site} {industry}" for industry in industries]
    base += [f"site:{site} {role}" for role in roles]
    return tuple(base)


WORKDAY = AtsConfig(
    name="workday",
    queries=_site_query_set("myworkdayjobs.com"),
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
    queries=_site_query_set("boards.greenhouse.io") + _site_query_set("job-boards.greenhouse.io"),
    url_regex=re.compile(
        r"https?://(?:job-)?boards\.greenhouse\.io/([a-zA-Z0-9_-]+)"
    ),
    extract_slug=lambda m: m.group(1).lower(),
    # Greenhouse public jobs API: GET returns {"jobs": [...]}.
    # We accept empty boards too — a company may have no openings
    # today but post next week; the slug is still valid for the
    # publisher to track. The presence of the ``jobs`` key (vs a
    # 404 / generic landing page) is enough proof the board exists.
    verify_url=lambda slug: f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    verify_ok=lambda r: (
        r.status_code == 200
        and isinstance(r.json(), dict)
        and "jobs" in r.json()
    ),
)

LEVER = AtsConfig(
    name="lever",
    queries=_site_query_set("jobs.lever.co"),
    url_regex=re.compile(r"https?://jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
    extract_slug=lambda m: m.group(1),  # Lever slugs ARE case-sensitive
    # Lever public API: GET returns a JSON array of postings. We
    # accept empty arrays — the board itself is real (Lever 404s
    # for nonexistent slugs).
    verify_url=lambda slug: f"https://api.lever.co/v0/postings/{slug}?mode=json",
    verify_ok=lambda r: r.status_code == 200 and isinstance(r.json(), list),
)

ASHBY = AtsConfig(
    name="ashby",
    queries=_site_query_set("jobs.ashbyhq.com"),
    url_regex=re.compile(r"https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
    extract_slug=lambda m: m.group(1).lower(),
    # Ashby's public job-board API returns board metadata + jobs.
    # We accept empty boards too — board existence (vs 404) is the
    # signal we care about; the publisher just emits 0 rows for a
    # silent run, no harm.
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
                    # Workday's jobs endpoint is POST with an empty
                    # applied-facets body. Accept empty job lists —
                    # a real Workday tenant (vs a 404'd or relocated
                    # one) returns 200 with the ``jobPostings`` key
                    # even when there are no current openings.
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
