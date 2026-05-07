"""Brute-force tenant probing — Google-free fallback.

When SearXNG / upstream engines are CAPTCHA'd, the continuous
daemon stalls. This script gives us a parallel discovery path that
goes straight to each ATS's public API: take a wordlist of
candidate slugs, hit ``ats.verify_url(slug)`` for each, write
the 200s into the same SQLite state DB the daemon uses.

Wordlist comes from three sources, in order of yield-per-probe:

1. Published-CSV slugs from OTHER ATSes (a company that uses
   greenhouse may have a stale lever account; companies sometimes
   migrate ATSes and the old account stays alive).
2. The names of the candidate slugs already discovered for this ATS
   but not yet verified (i.e., extracted from non-tenant SearXNG
   results — those are already in the seen set with verified=0; we
   skip them).
3. A static wordlist of common short company-name fragments — ships
   in ``examples/data/slug_wordlist.txt`` if present, otherwise an
   inline fallback.

Running this is **safe to interleave with the daemon** — both write
to the same SQLite DB with ``ON CONFLICT DO UPDATE``.

Usage:

    # Inside the discoverer container, against the same volume:
    python examples/brute_force_probe.py --ats workable --max 5000

    # Or on the host pointing at the volume:
    python examples/brute_force_probe.py --ats bamboohr,workable,rippling \\
        --state-db /var/lib/discovery/state.db --max 2000

The script is single-pass — finishes when the wordlist is consumed.
Re-run periodically (or wire it into a cron) for incremental gains.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discover_tenants import ALL_ATS, AtsConfig  # noqa: E402

log = logging.getLogger("brute")


# Static fallback wordlist — common short company-name fragments
# observed across ATSes. Extend by editing or by dropping a richer
# list at examples/data/slug_wordlist.txt (one slug per line).
_FALLBACK_WORDLIST = """
acme adobe airbnb airtable amazon apple appian apple asana asurion
atlas atlassian aws azure babbel banco baidu bayer benchling biogen
boeing boltai booking braze brex brilliant brookfield bumble byrider
canon canva cariad caterpillar centene cerner chime cisco citi
clear clickup cloudflare coinbase columbia compass confluent corsair
crowdstrike cruise daimler databricks deepmind dell deloitte deepl
deere delta deliveroo digitalocean discord disney docusign doordash
dropbox dynatrace eBay edx electronicarts elastic elementai elsevier
eos epic equifax ericsson ernst esri etsy expedia experian
facebook factset faraday faire fanatics figma figura fivetran flexport
ford foursquare freshworks ftc fubo galenica game genesys gilead
gitlab github globant gmf goldman google govlab grafana grammarly
grayscale grubhub gucci gusto h&m halo hermes hertz hilton hinge
hitachi homedepot honda honeywell hpe hsbc huawei hubspot hudson
huggingface hulu humana huntington ibm ideo iheart impossible indeed
infosys instacart intel intercom intuit invitae jpmc kaiser kayak
kering keys kpmg kraft kroger lattice lazada legalzoom lego lemonade
lendingclub lenovo lifebot liftoff lime liquibase lloyds lockheed loft
loomly lowes ltb lufthansa lululemon lyft macys mailchimp manulife
marriott mars maven mckesson mckinsey medallia medium meijer meraki
metro michelin microcenter microsoft mit mitel modivcare monday morgan
motorola murex mybiz neiman nestle newrelic nike nintendo niro nokia
notion novartis nutanix nvidia ocado okta olo omers ondeck oneapp
openai oracle origami orsted otto outbrain outdoor pajamas palantir
panasonic pandora pansaff pantheon papaya patagonia paypal pearson
peloton pendo pepsi peraton perfect personio pfizer phase pinterest
pivot pixar plaid playstation plex pokerstars polestar populationone
postman postmates pratt prudential publix qualcomm rackspace rakuten
rapidapi reddit redfin renault revolut rivian roblox rocket rockwell
rolls rpa rsa rss salesforce samsung sanofi santander sap saudia sears
sephora servicenow sgs shazam shell shopify siemens sifive simply
sixt skype slack smarthr snap snapchat snapdocs snowflake softbank
sony southwest spacex spotify square ssrn standardchartered stanford
starbucks state statefarm stmicro stripe subway sumsub super swift
syngenta tableau target tata tcs telefonica telstra tencent terraform
tesco tesla tetra texas thomson three tiktok tinder tinkoff tlc
tmobile toyota traderepublic transat travelers truepill tubi tumblr
turkey twilio twitch twitter typeform uber ubereats ubisoft ubs
ucla unicef unisys united unity uplift ups usaa vanguard vans vault
vercel verily verizon viasat vimeo virgin visa vmware volkswagen
volvo vox warner wawa wayfair waymo wells westpac whatfix whatsapp
whirlpool whoop wikimedia willis wipro wise wix wmt wolters workday
xerox xiaomi yahoo yandex yelp yum zalando zappos zara zendesk
zerolend zillow zoom zscaler zynga
""".strip().split()


def load_wordlist(path: Path | None) -> list[str]:
    if path and path.exists():
        return [
            ln.strip().lower()
            for ln in path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    return [w.lower() for w in _FALLBACK_WORDLIST]


def slugs_from_published(csv_path: Path, exclude_ats: str) -> list[str]:
    """Pull every slug from companies/all.csv that's NOT already on
    the target ATS — these are good cross-ATS probe candidates."""
    if not csv_path.exists():
        return []
    out: set[str] = set()
    with csv_path.open() as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            ats = (row.get("ats") or "").strip()
            slug = (row.get("slug") or "").strip().lower()
            if ats and slug and ats != exclude_ats:
                # Strip whitespace and non-slug chars — the published
                # CSV has dirty data (display names with spaces).
                clean = "".join(c for c in slug if c.isalnum() or c in "-_")
                if clean and len(clean) <= 60:
                    out.add(clean)
    return sorted(out)


def already_seen(conn: sqlite3.Connection, ats_name: str) -> set[str]:
    rows = conn.execute(
        "SELECT slug FROM tenants WHERE ats = ?", (ats_name,)
    ).fetchall()
    return {r[0] for r in rows}


_WORKDAY_INSTANCES = ("wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd103")
_WORKDAY_SITEMAP_RE = re.compile(
    r"Sitemap:\s*https?://[\w-]+\.(wd\d+)\.myworkdayjobs\.com/([\w-]+)/siteMap\.xml",
    re.IGNORECASE,
)


def probe_workday(slug: str, *, timeout: float = 5.0) -> tuple[str, bool, str]:
    """Workday brute-force is non-trivial because each tenant's
    verify URL needs ``(slug, instance, site)`` and the site name
    is per-tenant (e.g. ``NVIDIAExternalCareerSite`` for nvidia).

    Solution: hit ``robots.txt`` on every plausible instance for
    the slug. Workday helpfully publishes a ``Sitemap:`` line that
    embeds the canonical ``(instance, site)`` pair. From there,
    the standard cxs API call confirms the tenant is live.

    Returns the daemon's standard ``(slug, ok, status)`` triple.
    On hit, the slug is also re-cached for future use via the
    daemon's _WORKDAY_TRIPLE_CACHE so the SearXNG path can use it.
    """
    instance = None
    site = None
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
            instance = m.group(1)
            site = m.group(2)
            break
    if not (instance and site):
        return (slug, False, "no_workday_sitemap")

    # Confirm via the cxs API — the same call the verify pass uses.
    api = (
        f"https://{slug}.{instance}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs"
    )
    try:
        r = httpx.post(
            api,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
    except Exception as exc:
        return (slug, False, type(exc).__name__)
    ok = (
        r.status_code == 200
        and isinstance(r.json(), dict)
        and "jobPostings" in r.json()
    )
    if ok:
        # Cache the triple so the SearXNG-based daemon's verify pass
        # (which would otherwise re-default to wd1) can reuse it.
        from discover_tenants import _WORKDAY_TRIPLE_CACHE
        _WORKDAY_TRIPLE_CACHE.setdefault(slug, (slug, instance, site))
    return (slug, ok, f"http_{r.status_code}")


def probe(ats: AtsConfig, slug: str, *, timeout: float = 10.0) -> tuple[str, bool, str]:
    if ats.name == "workday":
        return probe_workday(slug, timeout=timeout)
    url = ats.verify_url(slug)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            return (slug, ats.verify_ok(r), f"http_{r.status_code}")
    except Exception as exc:
        return (slug, False, type(exc).__name__)


def upsert(conn: sqlite3.Connection, ats: str, slug: str, ok: bool, status: str) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        """
        INSERT INTO tenants (ats, slug, first_seen, last_verified, verified, last_status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (ats, slug) DO UPDATE SET
            last_verified = excluded.last_verified,
            verified      = excluded.verified,
            last_status   = excluded.last_status
        """,
        (ats, slug, now, now, 1 if ok else 0, status),
    )


def run_for_ats(
    ats: AtsConfig,
    conn: sqlite3.Connection,
    candidates: list[str],
    *,
    concurrency: int,
    max_probes: int,
) -> tuple[int, int]:
    """Returns ``(probes, verified)``."""
    seen = already_seen(conn, ats.name)
    todo = [c for c in candidates if c not in seen][:max_probes]
    log.info(
        "%s: probing %d candidates (skipping %d already in DB)",
        ats.name, len(todo), len(candidates) - len(todo),
    )
    verified = 0
    probes = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(probe, ats, slug): slug for slug in todo}
        for fut in as_completed(futures):
            slug, ok, status = fut.result()
            upsert(conn, ats.name, slug, ok, status)
            probes += 1
            if ok:
                verified += 1
            if probes % 100 == 0:
                log.info(
                    "  %s: %d probed, %d verified so far", ats.name, probes, verified
                )
    log.info("%s: DONE — %d probed, %d verified", ats.name, probes, verified)
    return probes, verified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ats", default="all",
        help="Comma-separated ATS names, or 'all' (default).",
    )
    parser.add_argument(
        "--state-db", default="/var/lib/discovery/state.db",
        help="SQLite path (must match the daemon's STATE_DB).",
    )
    parser.add_argument(
        "--companies-csv", default="/var/lib/discovery/companies_all.csv",
        help="Published companies CSV — used as a slug source.",
    )
    parser.add_argument(
        "--wordlist", type=Path, default=None,
        help="Path to a slug-per-line wordlist. Ships an inline fallback if absent.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8,
        help="Parallel HTTP probes. Default 8.",
    )
    parser.add_argument(
        "--max", type=int, default=2000,
        help="Cap probes per ATS to avoid runaway runs. Default 2000.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.ats == "all":
        ats_names = list(ALL_ATS)
    else:
        ats_names = [a.strip() for a in args.ats.split(",") if a.strip()]
        bad = [a for a in ats_names if a not in ALL_ATS]
        if bad:
            log.error("unknown ATS(es): %s", bad)
            return 1

    wordlist = load_wordlist(args.wordlist)
    log.info("wordlist size: %d", len(wordlist))

    conn = sqlite3.connect(args.state_db, isolation_level=None)

    overall_probes = 0
    overall_verified = 0
    started = time.time()
    for name in ats_names:
        ats = ALL_ATS[name]
        # Cross-pollinate: for ATS X, probe slugs known to other ATSes.
        cross = slugs_from_published(Path(args.companies_csv), exclude_ats=name)
        # Combine: dedupe, keep order (cross-pollination first, then wordlist).
        seen = set()
        candidates = []
        for s in cross + wordlist:
            if s and s not in seen:
                seen.add(s)
                candidates.append(s)
        p, v = run_for_ats(
            ats, conn, candidates,
            concurrency=args.concurrency, max_probes=args.max,
        )
        overall_probes += p
        overall_verified += v

    elapsed = time.time() - started
    log.info(
        "DONE — %d total probes, %d verified, %.1fs (%.0f probes/s)",
        overall_probes, overall_verified, elapsed,
        overall_probes / elapsed if elapsed else 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
