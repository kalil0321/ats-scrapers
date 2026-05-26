#!/usr/bin/env python3
"""Generic pipeline runner: scrape every tenant of an ATS and write one CSV.

Used by ``full_pipeline.sh`` for ATSes that don't have a legacy
``data/{ats}/main.py`` — scrapers that live only in jobhive.

Reads ``ats-companies/{ats}.csv`` (the canonical tenant list — single
source of truth, columns ``name,url``), scrapes each tenant via the
appropriate jobhive class, dedupes, and writes a flat
``data/{ats}/jobs.csv``.

Usage:
    python scripts/run_pipeline.py cornerstone
    python scripts/run_pipeline.py icims --concurrency 6
    python scripts/run_pipeline.py breezy --max-tenants 50
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jobhive.exceptions import CompanyNotFoundError
from jobhive.models import Job
from jobhive.scrapers import (
    AmazonScraper,
    AppleScraper,
    ArbetsformedlingenScraper,
    AshbyScraper,
    AvatureScraper,
    BambooHRScraper,
    BreezyScraper,
    BuiltInScraper,
    BundesagenturScraper,
    CornerstoneScraper,
    EightfoldScraper,
    EuresScraper,
    GemScraper,
    GetOnBrdScraper,
    GoogleScraper,
    GreenhouseScraper,
    JazzHRScraper,
    JobsChScraper,
    JoinComScraper,
    LeverScraper,
    ManfredScraper,
    MercorScraper,
    MetaScraper,
    OracleScraper,
    PersonioScraper,
    PhenomScraper,
    PinpointScraper,
    ProgramathorScraper,
    RecruiteeScraper,
    RecruiterboxScraper,
    RemoteOKScraper,
    RipplingScraper,
    SmartRecruitersScraper,
    SuccessFactorsScraper,
    TaleoScraper,
    TeamtailorScraper,
    TeslaScraper,
    TheHubScraper,
    TikTokScraper,
    UberScraper,
    WantedScraper,
    WellfoundScraper,
    WeWorkRemotelyScraper,
    WorkableScraper,
    WorkdayScraper,
    YCombinatorScraper,
    iCIMSScraper,
)
from jobhive.scrapers.base import BaseScraper


def _slug_col(row: dict[str, Any]) -> str | None:
    """Return the canonical ``slug`` column value if present and non-empty.

    Introduced by the 2026-05 ``ats-companies/`` migration: the CSV now
    carries an explicit ``slug`` column with the scraper/API identifier
    (decoupled from ``url`` which is the user-facing canonical URL).
    All slug-extractor helpers and lambdas below prefer this column
    first, falling back to the legacy url/name parsing logic so files
    that haven't been migrated yet still work.
    """
    slug = (row.get("slug") or "").strip()
    return slug or None


def _recruitee_slug(row: dict[str, Any]) -> str | None:
    """Recruitee tenants live at ``{slug}.recruitee.com``. CSVs sometimes
    store the human-readable name in the ``name`` column and the slug in
    the URL — always parse the URL when present."""
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://([a-z0-9][a-z0-9-]+)\.recruitee\.com", url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _personio_slug(row: dict[str, Any]) -> str | None:
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://([a-z0-9][a-z0-9-]+)\.jobs\.personio\.com",
                     url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _avature_slug(row: dict[str, Any]) -> str | None:
    """Avature tenants live at ``{slug}.avature.net`` (or sometimes the
    full careers URL). Extract the subdomain when a URL is present."""
    if (slug := _slug_col(row)):
        if slug.startswith(("http://", "https://")):
            return slug
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://([a-z0-9][a-z0-9-]+)\.avature\.net",
                     url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _successfactors_slug(row: dict[str, Any]) -> str | None:
    """SuccessFactors tenants are best addressed by their careers host.

    The explicit ``slug`` column is useful for stable tenant identity, but it
    is not necessarily a resolvable host. After the 2026-05 CSV migration rows
    like ``slug=ace1950`` also carry ``url=https://ace1950.jobs2web.com``; the
    scraper needs the latter to avoid guessing ``job.ace1950.com``.
    """
    url = (row.get("url") or "").strip()
    if url:
        return url.rstrip("/")
    if (slug := _slug_col(row)):
        return slug
    return (row.get("name") or "").strip() or None


def _rippling_slug(row: dict[str, Any]) -> str | None:
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://ats\.rippling\.com/([a-z0-9][a-z0-9-]+)",
                     url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _workable_slug(row: dict[str, Any]) -> str | None:
    # Workable tenants are case-sensitive in the URL but lowercased
    # in the API; ``apply.workable.com`` is permissive. Preserve case
    # from the slug column when present.
    if (slug := _slug_col(row)):
        return slug
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://apply\.workable\.com/([^/?#]+)",
                     url, re.IGNORECASE)
        if m:
            return m.group(1)
    name = (row.get("name") or "").strip()
    return name or None


def _lever_slug(row: dict[str, Any]) -> str | None:
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://jobs\.lever\.co/([^/?#]+)", url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _greenhouse_slug(row: dict[str, Any]) -> str | None:
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(
            r"https?://(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)",
            url, re.IGNORECASE,
        )
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _ashby_slug(row: dict[str, Any]) -> str | None:
    if (slug := _slug_col(row)):
        return slug.lower()
    url = (row.get("url") or "").strip()
    if url.startswith("http"):
        m = re.match(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    name = (row.get("name") or "").strip()
    return name or None


def _oracle_slug(row: dict[str, Any]) -> str | None:
    """Return the Oracle API origin plus site selector.

    ``ats-companies/oracle.csv`` stores user-facing CandidateExperience URLs,
    e.g. ``https://host/hcmUI/CandidateExperience/en/sites/CX_1``. The scraper
    calls REST endpoints at the host root and needs the site number separately.
    """
    raw = (row.get("url") or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return raw
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    base = f"{parsed.scheme}://{parsed.netloc}"
    site = _oracle_site_from_url(raw)
    return f"{base}?site_number={site}" if site else base


def _oracle_site_from_url(raw: str) -> str | None:
    parsed = urlparse(raw)
    query_site = parse_qs(parsed.query).get("site_number")
    if query_site and query_site[0]:
        return query_site[0]
    match = re.search(r"/sites/([^/?#]+)", parsed.path)
    if match:
        return match.group(1)
    return None


def _icims_slug(row: dict[str, Any]) -> str | None:
    """iCIMS rows can have a bare slug in `name`/`slug` or a
    `careers-{slug}.icims.com` URL. Either form is accepted by the
    scraper, but normalize to slug. Keep parsing the URL first when
    present because some tenants use the `uscareers-` subdomain
    variant — that information lives in the URL, not the slug column."""
    url = (row.get("url") or "").strip()
    if url:
        m = re.search(r"//careers-([a-z0-9-]+)\.icims\.com", url)
        if m:
            return m.group(1)
        m = re.search(r"//uscareers-([a-z0-9-]+)\.icims\.com", url)
        if m:
            # Pass the full URL so the scraper preserves the uscareers- prefix.
            return url.split("?", 1)[0].rstrip("/")
    if (slug := _slug_col(row)):
        return slug
    return (row.get("name") or "").strip() or None


# Per-ATS config:
# - scraper: the jobhive class
# - slug: callable turning a CSV row into `company_slug`
# - kwargs (optional): callable returning additional kwargs for the scraper
#   (used by Phenom which needs `locale` and `country` per tenant)
# - csv: tenant CSV path (relative to repo root; canonical location
#   is ``ats-companies/{ats}.csv`` with columns ``name,url``)
# - output: jobs CSV output path (per-ATS jobs dataset under ``{ats}/``)
CONFIGS: dict[str, dict[str, Any]] = {
    "cornerstone": {
        "scraper": CornerstoneScraper,
        # Scraper accepts either a bare slug OR the full career URL.
        # Prefer the slug column post-migration.
        "slug": lambda r: _slug_col(r) or r.get("url") or r.get("name"),
        "kwargs": lambda r: {
            "company_name": (r.get("name") or "").strip() or None,
        },
        "csv": "ats-companies/cornerstone.csv",
        "output": "cornerstone/jobs.csv",
    },
    "icims": {
        "scraper": iCIMSScraper,
        "slug": _icims_slug,
        "csv": "ats-companies/icims.csv",
        "output": "icims/jobs.csv",
    },
    "breezy": {
        "scraper": BreezyScraper,
        "slug": lambda r: _slug_col(r) or r.get("name"),
        "csv": "ats-companies/breezy.csv",
        "output": "breezy/jobs.csv",
    },
    "gem": {
        # Gem boards live at ``jobs.gem.com/{slug}``. Post-migration the
        # slug column has the value directly; legacy files store it in the
        # last URL path component, which we extract as fallback.
        "scraper": GemScraper,
        "slug": lambda r: _slug_col(r) or (
            (r.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if (r.get("url") or "").strip()
            else (r.get("name") or "").strip()
        ),
        "csv": "ats-companies/gem.csv",
        "output": "gem/jobs.csv",
    },
    "successfactors": {
        "scraper": SuccessFactorsScraper,
        "slug": _successfactors_slug,
        "csv": "ats-companies/successfactors.csv",
        "output": "successfactors/jobs.csv",
    },
    "taleo": {
        "scraper": TaleoScraper,
        # Taleo CSV stores bare URLs without scheme (the discovery flow
        # captures slug=full URL). The scraper needs `https://` prefix.
        "slug": lambda r: (
            (r.get("url") or "").strip()
            if (r.get("url") or "").startswith("http")
            else f"https://{(r.get('url') or '').strip()}"
            if r.get("url") else None
        ),
        "csv": "ats-companies/taleo.csv",
        "output": "taleo/jobs.csv",
    },
    "oracle": {
        "scraper": OracleScraper,
        "slug": _oracle_slug,
        "csv": "ats-companies/oracle.csv",
        "output": "oracle/jobs.csv",
    },
    "phenom": {
        "scraper": PhenomScraper,
        "slug": lambda r: r.get("url"),
        # Phenom needs per-tenant locale + country.
        "kwargs": lambda r: {
            "locale": r.get("locale") or "en_us",
            "country": r.get("country") or "us",
        },
        "csv": "ats-companies/phenom.csv",
        "output": "phenom/jobs.csv",
    },
    "pinpoint": {
        "scraper": PinpointScraper,
        "slug": lambda r: _slug_col(r) or r.get("name") or r.get("url"),
        "csv": "ats-companies/pinpoint.csv",
        "output": "pinpoint/jobs.csv",
    },
    "recruiterbox": {
        "scraper": RecruiterboxScraper,
        "slug": lambda r: _slug_col(r) or r.get("name") or r.get("url"),
        "csv": "ats-companies/recruiterbox.csv",
        "output": "recruiterbox/jobs.csv",
    },
    "workday": {
        # Workday's slug is the FULL careers URL (the scraper parses the
        # company/instance/site components). The CSV stores `url` directly.
        "scraper": WorkdayScraper,
        "slug": lambda r: (r.get("url") or "").strip() or None,
        "kwargs": lambda r: {
            "max_fetch_seconds": float(
                os.environ.get("JOBHIVE_WORKDAY_TENANT_TIMEOUT", "900")
            ),
            "company_name": (r.get("name") or "").strip() or None,
        },
        "csv": "ats-companies/workday.csv",
        "output": "workday/jobs.csv",
        # Workday descriptions require per-job detail calls. Defer to the
        # disk-backed description cache so the scraper's internal enrichment
        # stays off (it would re-fetch every row, bypassing the cache).
        "defer_descriptions_to_cache": True,
        # Persistent zstd-compressed SQLite cache. ~700k entries from the
        # 2026-05 backfill seed the file; daily runs hit the cache for already
        # known URLs and only fetch the detail endpoint for new listings.
        "description_cache_path": "workday/descriptions.sqlite3",
        "description_cache_compress": True,
        # Workday can take longer than the publish window on bad API days. Keep
        # publishing the previous stable jobs.csv while a replacement is built.
        "publish_previous_while_running": True,
    },
    "bamboohr": {
        "scraper": BambooHRScraper,
        "slug": lambda r: _slug_col(r) or (r.get("name") or "").strip() or None,
        "csv": "ats-companies/bamboohr.csv",
        "output": "bamboohr/jobs.csv",
    },
    "teamtailor": {
        "scraper": TeamtailorScraper,
        "slug": lambda r: _slug_col(r) or (r.get("name") or "").strip() or None,
        "csv": "ats-companies/teamtailor.csv",
        "output": "teamtailor/jobs.csv",
    },
    "jazzhr": {
        "scraper": JazzHRScraper,
        # JazzHR sites are Cloudflare-protected — the scraper auto-falls
        # back to httpcloak under client_kind="auto".
        "slug": lambda r: _slug_col(r) or (r.get("name") or "").strip() or None,
        "csv": "ats-companies/jazzhr.csv",
        "output": "jazzhr/jobs.csv",
    },
    "recruitee": {
        "scraper": RecruiteeScraper,
        # Recruitee CSVs mix human names ("5280 High School") with the
        # actual subdomain ("5280highschool"). Always derive the slug
        # from the URL when one is present.
        "slug": _recruitee_slug,
        "csv": "ats-companies/recruitee.csv",
        "output": "recruitee/jobs.csv",
    },
    "ashby": {
        "scraper": AshbyScraper,
        "slug": _ashby_slug,
        "csv": "ats-companies/ashby.csv",
        "output": "ashby/jobs.csv",
    },
    "lever": {
        "scraper": LeverScraper,
        "slug": _lever_slug,
        "csv": "ats-companies/lever.csv",
        "output": "lever/jobs.csv",
    },
    "greenhouse": {
        "scraper": GreenhouseScraper,
        "slug": _greenhouse_slug,
        "csv": "ats-companies/greenhouse.csv",
        "output": "greenhouse/jobs.csv",
    },
    "workable": {
        "scraper": WorkableScraper,
        "slug": _workable_slug,
        "csv": "ats-companies/workable.csv",
        "output": "workable/jobs.csv",
    },
    "smartrecruiters": {
        "scraper": SmartRecruitersScraper,
        # SmartRecruiters slugs are case-sensitive (e.g. ``Dominos``,
        # not ``dominos``). Both the legacy ``name`` column and the new
        # ``slug`` column must preserve case — never call ``.lower()``
        # on them. If the slug column got lowercased by accident, fall
        # back to the name column which is canonically capitalized.
        "slug": lambda r: _slug_col(r) or (r.get("name") or "").strip() or None,
        "csv": "ats-companies/smartrecruiters.csv",
        "output": "smartrecruiters/jobs.csv",
    },
    "personio": {
        "scraper": PersonioScraper,
        "slug": _personio_slug,
        "csv": "ats-companies/personio.csv",
        "output": "personio/jobs.csv",
    },
    "rippling": {
        "scraper": RipplingScraper,
        "slug": _rippling_slug,
        "csv": "ats-companies/rippling.csv",
        "output": "rippling/jobs.csv",
    },
    "avature": {
        "scraper": AvatureScraper,
        "slug": _avature_slug,
        "csv": "ats-companies/avature.csv",
        "output": "avature/jobs.csv",
    },
    "join_com": {
        "scraper": JoinComScraper,
        # Prefer the canonical slug column; legacy fallback derives it
        # from the URL's last path component. The lowercased form
        # matters because names in the CSV come from the sitemap with
        # arbitrary casing and would 301-redirect on every probe.
        "slug": lambda r: (
            (_slug_col(r) or "").lower()
            or (r.get("url") or "").rstrip("/").rsplit("/", 1)[-1].lower()
            or None
        ),
        "csv": "ats-companies/join_com.csv",
        "output": "join_com/jobs.csv",
    },
    "mercor": {
        "scraper": MercorScraper,
        # Mercor is a single-tenant scraper — slug is ignored.
        "slug": lambda r: "mercor",
        "csv": "ats-companies/mercor.csv",
        "output": "mercor/jobs.csv",
    },
    # ---- Single-tenant big-tech scrapers (singleton mode) -----------------
    # Each big-tech employer runs its own bespoke careers system. These
    # scrapers ignore the slug (or use a fixed one) — wire them through
    # the runner via ``singleton: True`` so we don't need a one-row CSV
    # per company. Output goes to ``{ats}/jobs.csv``.
    "amazon": {
        "scraper": AmazonScraper, "singleton": True,
        "output": "amazon/jobs.csv",
    },
    "apple": {
        "scraper": AppleScraper, "singleton": True,
        "output": "apple/jobs.csv",
    },
    "google": {
        "scraper": GoogleScraper, "singleton": True,
        "output": "google/jobs.csv",
    },
    "meta": {
        "scraper": MetaScraper, "singleton": True,
        "output": "meta/jobs.csv",
    },
    "tesla": {
        "scraper": TeslaScraper, "singleton": True,
        "output": "tesla/jobs.csv",
    },
    "tiktok": {
        "scraper": TikTokScraper, "singleton": True,
        "output": "tiktok/jobs.csv",
    },
    "uber": {
        "scraper": UberScraper, "singleton": True,
        "output": "uber/jobs.csv",
    },
    "bundesagentur": {
        # German federal employment agency — official, public, ~1M+ jobs.
        # Single-source aggregator; subdivides internally by berufsfeld
        # (job category) to bypass the 10k pagination cap.
        "scraper": BundesagenturScraper, "singleton": True,
        "output": "bundesagentur/jobs.csv",
    },
    "arbetsformedlingen": {
        # Sweden's federal employment service — public JSON API. ~46k
        # active jobs, fanned out across 21 regions to bypass the 10k cap.
        "scraper": ArbetsformedlingenScraper, "singleton": True,
        "output": "arbetsformedlingen/jobs.csv",
    },
    "eures": {
        # EU-wide aggregator across the 31 EURES countries. ~2.7M jobs;
        # subdivided per-country, then by NUTS region / NACE sector if a
        # country exceeds the 10k pagination cap.
        "scraper": EuresScraper, "singleton": True,
        "output": "eures/jobs.csv",
        # The pre-detail-fallback EURES CSV truncated descriptions at
        # 500 chars. Do not reuse that legacy file as a cache, otherwise
        # a full rerun would just preserve the truncated descriptions.
        "skip_description_cache_if_max_len_lte": 500,
    },
    # Direct-posting providers from PR #15 — each is a single global
    # endpoint (slug ignored). Pass any value (e.g. ``"any"``) when
    # invoking the scraper; the runner uses ``singleton: True`` so no
    # per-tenant CSV is required.
    "builtin": {
        # US tech jobs aggregator. ~3-6k live jobs depending on the day.
        "scraper": BuiltInScraper, "singleton": True,
        "output": "builtin/jobs.csv",
    },
    "getonbrd": {
        # LATAM tech jobs board. ~1k live.
        "scraper": GetOnBrdScraper, "singleton": True,
        "output": "getonbrd/jobs.csv",
    },
    "jobsch": {
        # jobs.ch — Switzerland's largest direct-posting board. ~50k live.
        "scraper": JobsChScraper, "singleton": True,
        "output": "jobsch/jobs.csv",
    },
    "manfred": {
        # Manfred — Spain / LATAM curated tech roles. ~40 live.
        "scraper": ManfredScraper, "singleton": True,
        "output": "manfred/jobs.csv",
    },
    "programathor": {
        # Programathor — Brazilian dev jobs. ~3k live, opt-in proxy
        # path for the bot-protected detail pages.
        "scraper": ProgramathorScraper, "singleton": True,
        "output": "programathor/jobs.csv",
    },
    "remoteok": {
        # RemoteOK — remote-only listings, US-heavy. ~100 live.
        "scraper": RemoteOKScraper, "singleton": True,
        "output": "remoteok/jobs.csv",
    },
    "thehub": {
        # The Hub — Nordic startups, ships lat/lon. ~1k live.
        "scraper": TheHubScraper, "singleton": True,
        "output": "thehub/jobs.csv",
    },
    "wanted": {
        # Wanted — Korea + Japan tech roles. ~10k live.
        "scraper": WantedScraper, "singleton": True,
        "output": "wanted/jobs.csv",
    },
    "wellfound": {
        # Wellfound (was AngelList Talent) — US startups. ~700 live;
        # opt-in Firecrawl path because the API is auth-gated.
        "scraper": WellfoundScraper, "singleton": True,
        "output": "wellfound/jobs.csv",
    },
    "weworkremotely": {
        # We Work Remotely — remote-only listings. ~500 live.
        "scraper": WeWorkRemotelyScraper, "singleton": True,
        "output": "weworkremotely/jobs.csv",
    },
    "ycombinator": {
        # Y Combinator's Work at a Startup board. ~770 live.
        "scraper": YCombinatorScraper, "singleton": True,
        "output": "ycombinator/jobs.csv",
    },
    "eightfold": {
        "scraper": EightfoldScraper,
        # CSV has either a slug (most tenants → ``{slug}.eightfold.ai``) or a
        # full custom-domain URL (``apply.careers.{co}.com``). Pass the slug
        # column verbatim; for full URLs we extract the slug and let the
        # ``base_url`` kwarg do the override.
        "slug": lambda r: (
            (r.get("slug") or r.get("url") or r.get("name") or "")
            .strip()
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
            .split(".")[0]
            or None
        ),
        "kwargs": lambda r: _eightfold_kwargs(r),
        "csv": "ats-companies/eightfold.csv",
        "output": "eightfold/jobs.csv",
    },
}


def _eightfold_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    """Build Eightfold-specific overrides from a CSV row.

    - ``url`` (when full https://...) → ``base_url`` override (custom domains).
    - ``domain`` column → API ``domain`` parameter.
    - Otherwise default to ``{slug}.eightfold.ai`` and ``{slug}.com``.
    """
    kw: dict[str, Any] = {}
    raw_url = (row.get("url") or "").strip()
    if raw_url.startswith("http"):
        parsed = urlparse(raw_url)
        if parsed.scheme and parsed.netloc:
            kw["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
        else:
            kw["base_url"] = raw_url.rstrip("/")
    domain = (row.get("domain") or "").strip()
    if domain:
        kw["domain"] = domain
    name = (row.get("name") or "").strip()
    if name:
        kw["company_name"] = name
    return kw

DATA_ROOT = Path(__file__).resolve().parent.parent  # → repo root

JOB_CSV_FIELDS = [
    "url", "title", "company", "ats_type", "ats_id", "location",
    "is_remote", "salary_min", "salary_max", "salary_currency",
    "salary_period", "salary_summary", "employment_type",
    "department", "team", "description", "posted_at",
    "requisition_id", "apply_url", "commitment", "raw",
]
STREAM_DESCRIPTION_CONCURRENCY = 8


@contextmanager
def _pipeline_lock(ats: str):
    """Prevent concurrent runs of the same ATS pipeline.

    Cron can start a new daily run while a previous long runner is still
    writing `{ats}/.jobs.csv.tmp`. The publish step correctly refuses to
    publish while that temp output exists, so overlapping runs can block
    deployment for days. `flock` releases automatically if the process dies.
    """
    lock_path = Path(tempfile.gettempdir()) / f"jobhive-run-pipeline-{ats}.lock"
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            owner = fh.read().strip() or "unknown pid"
            print(f"[{ats}] another run is already active ({owner}); skipping.")
            yield False
            return
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        fh.flush()
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# Schema layout version stored in SQLite's PRAGMA user_version. Bumping
# this number tells the cache to refuse opening an older file whose row
# format we no longer know how to decode (e.g. legacy ``description
# TEXT`` rows that would silently come back as ``str`` through the BLOB
# read path and crash zstd decompression).
_CACHE_SCHEMA_VERSION = 2


class DescriptionCache:
    """Disk-backed description cache, optionally persistent and zstd-compressed.

    Two modes:

    - **Ephemeral** (default): ``DescriptionCache()`` creates a tempfile that
      gets removed on :meth:`close`. Behavior matches the legacy single-run
      cache rebuilt each pipeline invocation via :meth:`load_csv`.

    - **Persistent**: ``DescriptionCache(path=Path("...sqlite3"))`` opens (or
      creates) the named file and keeps it across runs. Use for ATSes where
      rebuilding from CSV every day is wasteful — e.g. Workday at ~700k
      entries. New fetches accumulate in-place via :meth:`set`.

    ``compress=True`` stores the description column as zstd-compressed BLOB.
    Cuts disk footprint by ~70% on typical HTML/markdown text. Read overhead
    is single-digit milliseconds per lookup, which is dwarfed by the network
    fetch cost it replaces.
    """

    def __init__(self, path: Path | None = None, compress: bool = False) -> None:
        self.conn: sqlite3.Connection | None = None
        self.compress = compress
        self._compressor = None
        self._decompressor = None
        if compress:
            import zstandard
            self._compressor = zstandard.ZstdCompressor(level=3)
            self._decompressor = zstandard.ZstdDecompressor()

        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix="jobhive-description-cache-",
                suffix=".sqlite3",
                delete=False,
            ) as tmp:
                self.path = Path(tmp.name)
            self._owns_tempfile = True
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._owns_tempfile = False

        try:
            self.conn = sqlite3.connect(self.path)
            if self._owns_tempfile:
                # Ephemeral tempfile cache — favor raw insert speed over crash
                # safety (file is discarded on close anyway).
                self.conn.execute("PRAGMA journal_mode=OFF")
                self.conn.execute("PRAGMA synchronous=OFF")
            else:
                # Persistent cache — WAL gives concurrent reader safety while
                # writes stay durable. NORMAL sync is sufficient for our
                # daily-replay-on-loss model.
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            # Schema-version pragma. Bump when the table layout or encoding
            # changes incompatibly so an older persistent file fails loudly
            # at open instead of silently mixing schemas. Version 1 is the
            # original ``description TEXT`` layout; version 2 introduced
            # ``description BLOB`` to carry optionally-zstd-compressed bytes.
            current_user_version = self.conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            existing_rows = 0
            existing_table = self.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='descriptions'"
            ).fetchone()
            if existing_table is not None:
                existing_rows = self.conn.execute(
                    "SELECT COUNT(*) FROM descriptions"
                ).fetchone()[0]
            if existing_rows > 0 and current_user_version != _CACHE_SCHEMA_VERSION:
                # Bail loudly rather than try to interpret an unknown layout
                # — silently returning TEXT bytes through the BLOB path
                # leaks ``str`` rows that then crash _decode().
                if self.conn is not None:
                    self.conn.close()
                    self.conn = None
                if self._owns_tempfile:
                    self.path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"DescriptionCache schema mismatch at {self.path}: "
                    f"file user_version={current_user_version}, "
                    f"code expects {_CACHE_SCHEMA_VERSION}. Delete the "
                    f"file and let the pipeline reseed it from the "
                    f"current jobs.csv (or run scripts/build_workday_cache "
                    f"if a backfill seed is available)."
                )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS descriptions (
                    kind TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    description BLOB NOT NULL,
                    PRIMARY KEY (kind, cache_key)
                )
                """
            )
            self.conn.execute(
                f"PRAGMA user_version = {_CACHE_SCHEMA_VERSION}"
            )
            self.count = self.conn.execute(
                "SELECT COUNT(*) FROM descriptions"
            ).fetchone()[0]
        except Exception:
            if self.conn is not None:
                self.conn.close()
            if self._owns_tempfile:
                self.path.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self._owns_tempfile:
            self.path.unlink(missing_ok=True)

    def _encode(self, description: str) -> bytes:
        raw = description.encode("utf-8")
        return self._compressor.compress(raw) if self.compress else raw

    def _decode(self, blob: bytes) -> str:
        raw = self._decompressor.decompress(blob) if self.compress else blob
        return raw.decode("utf-8")

    def load_csv(self, path: Path) -> None:
        if not path.exists():
            return

        batch: list[tuple[str, str, bytes]] = []
        try:
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    description = (row.get("description") or "").strip()
                    if not description:
                        continue
                    blob = self._encode(description)
                    for key in _row_description_keys(row):
                        batch.append((*key, blob))
                    if len(batch) >= 2_000:
                        self._insert_many(batch)
                        batch.clear()
                if batch:
                    self._insert_many(batch)
        except (OSError, csv.Error, sqlite3.Error):
            self.conn.execute("DELETE FROM descriptions")
            self.conn.commit()
        self.count = self.conn.execute(
            "SELECT COUNT(*) FROM descriptions"
        ).fetchone()[0]

    def _insert_many(self, rows: list[tuple[str, str, bytes]], *, replace: bool = False) -> int:
        """Bulk insert. ``replace=True`` overwrites existing rows
        (used by :meth:`set` so an updated description from a fresh
        scrape supersedes the previous-day cached one); the default
        ``replace=False`` keeps the existing row, which is what
        :meth:`load_csv` wants when seeding from a CSV that may have
        the same URL listed multiple times.
        """
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        cur = self.conn.executemany(
            f"""
            {verb} INTO descriptions (kind, cache_key, description)
            VALUES (?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return cur.rowcount

    def get(self, job: Job) -> str | None:
        for kind, key in _description_keys(job):
            row = self.conn.execute(
                """
                SELECT description FROM descriptions
                WHERE kind = ? AND cache_key = ?
                """,
                (kind, key),
            ).fetchone()
            if row:
                return self._decode(row[0])
        return None

    def set(self, job: Job, description: str) -> None:
        blob = self._encode(description)
        rows = [(*key, blob) for key in _description_keys(job)]
        if not rows:
            return
        # Single-row updates from _ensure_description must replace any
        # existing entry — that's the whole point of writing back when
        # the freshly-scraped body is longer than the cached one. The
        # rowcount returned by executemany with INSERT OR REPLACE counts
        # both new inserts and updates, so we only bump ``count`` by
        # the genuine new keys (those not already present).
        new_keys = 0
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM descriptions WHERE (kind, cache_key) IN ("
            + ",".join("(?,?)" for _ in rows)
            + ")",
            [v for kind, key, _ in rows for v in (kind, key)],
        ).fetchone()[0]
        new_keys = len(rows) - existing
        self._insert_many(rows, replace=True)
        self.count += max(0, new_keys)


def _description_keys(job: Job) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    company = (job.company or "").strip()
    ats_id = (job.ats_id or "").strip()
    if company and ats_id:
        keys.append(("company_ats_id", f"{company}\0{ats_id}"))
    url = str(job.url).strip()
    if url:
        keys.append(("url", url))
    return keys


def _row_description_keys(row: dict[str, str]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    company = (row.get("company") or "").strip()
    ats_id = (row.get("ats_id") or "").strip()
    if company and ats_id:
        keys.append(("company_ats_id", f"{company}\0{ats_id}"))
    url = (row.get("url") or "").strip()
    if url:
        keys.append(("url", url))
    return keys


def _load_description_cache(
    path: Path,
    *,
    persistent_path: Path | None = None,
    compress: bool = False,
    bootstrap_csv: Path | None = None,
) -> DescriptionCache:
    """Open a description cache, ephemeral or persistent.

    - ``persistent_path``: when set, opens the SQLite file at that path and
      reuses it across runs. Otherwise builds a fresh tempfile cache from
      ``path``.
    - ``compress``: forwarded to :class:`DescriptionCache`. Recommended for
      persistent caches with large descriptions (e.g. Workday).
    - ``bootstrap_csv``: if the persistent cache is empty on open, seed it
      from this CSV. Useful for the first run after enabling persistence.
    """
    if persistent_path is not None:
        cache = DescriptionCache(path=persistent_path, compress=compress)
        if cache.count == 0 and bootstrap_csv is not None:
            try:
                cache.load_csv(bootstrap_csv)
            except Exception:
                cache.close()
                raise
        return cache
    cache = DescriptionCache(compress=compress)
    try:
        cache.load_csv(path)
    except Exception:
        cache.close()
        raise
    return cache


def _descriptions_look_capped(path: Path, max_len: int) -> bool:
    if not path.exists():
        return False

    found_description = False
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                description = (row.get("description") or "").strip()
                if not description:
                    continue
                found_description = True
                if len(description) > max_len:
                    return False
    except (OSError, csv.Error):
        return False
    return found_description


def _cached_description(job: Job, cache: DescriptionCache) -> str | None:
    return cache.get(job)


async def _ensure_description(
    scraper: BaseScraper,
    job: Job,
    cache: DescriptionCache,
) -> str:
    cached = _cached_description(job, cache)
    fresh = job.description
    if cached:
        # Prefer the longer description. The cache is the previous run's
        # jobs.csv (or a persistent SQLite). When the scraper has already
        # populated ``job.description`` from the current API (e.g. lever,
        # which assembles ``description`` + ``lists[]`` in _parse_job,
        # or apple which hydrates per-job detail pages), a recent code
        # update may produce a fuller body than the previously-cached
        # one. Trust whichever has more content; ties go to fresh.
        if not fresh or len(cached) > len(fresh):
            job.description = cached
            return "cache"
        # Fresh is at least as long — keep it AND write it back to the
        # cache so the next run picks up the improvement immediately
        # instead of recomputing it.
        cache.set(job, fresh)
        return "present"
    if fresh:
        return "present"
    try:
        description = await asyncio.to_thread(scraper.get_description, job)
    except Exception as exc:
        print(
            "  description fetch failed for "
            f"{job.url}: {type(exc).__name__}: {str(exc)[:200]}"
        )
        return "error"
    if description:
        job.description = description[:25_000]
        cache.set(job, job.description)
        return "fetched"
    return "missing"


def _job_to_row(job: Job) -> dict[str, Any]:
    """Flatten a Job to CSV-friendly fields. ``raw`` is JSON-serialized."""
    raw_json = ""
    if job.raw:
        try:
            raw_json = json.dumps(job.raw, default=str, ensure_ascii=False)[:5000]
        except (TypeError, ValueError):
            raw_json = ""
    return {
        "url": str(job.url),
        "title": job.title,
        "company": job.company,
        "ats_type": job.ats_type.value,
        "ats_id": job.ats_id,
        "location": job.location or "",
        "is_remote": "" if job.is_remote is None else str(job.is_remote).lower(),
        "salary_min": "" if job.salary_min is None else job.salary_min,
        "salary_max": "" if job.salary_max is None else job.salary_max,
        "salary_currency": job.salary_currency or "",
        "salary_period": job.salary_period or "",
        "salary_summary": job.salary_summary or "",
        "employment_type": job.employment_type or "",
        "department": job.department or "",
        "team": job.team or "",
        "description": (job.description or "")[:25_000],
        "posted_at": job.posted_at.isoformat() if job.posted_at else "",
        "requisition_id": job.requisition_id or "",
        "apply_url": str(job.apply_url) if job.apply_url else "",
        "commitment": job.commitment or "",
        "raw": raw_json,
    }


async def _run_scraper(
    scraper_cls,
    slug,
    kwargs=None,
    timeout=30,
    *,
    include_descriptions: bool = True,
) -> tuple[str, BaseScraper | None, list[Job], str | None]:
    """Run one scraper in a thread (most are sync)."""
    extra = kwargs or {}

    def _run() -> tuple[BaseScraper, list[Job]]:
        scraper = scraper_cls(slug, timeout=timeout, **extra)
        scraper.include_descriptions = include_descriptions
        return scraper, scraper.fetch()

    try:
        scraper, jobs = await asyncio.to_thread(_run)
        return slug, scraper, jobs, None
    except CompanyNotFoundError:
        return slug, None, [], "not_found"
    except Exception as exc:
        return slug, None, [], f"{type(exc).__name__}: {str(exc)[:120]}"


async def run(ats: str, concurrency: int, max_tenants: int | None, timeout: float) -> int:
    cfg = CONFIGS[ats]

    targets: list[tuple[str, dict[str, Any]]] = []
    if cfg.get("singleton"):
        # Single-tenant scraper (big-tech bespoke careers). No CSV — call
        # the scraper once with the ATS name as a placeholder slug; the
        # scraper itself ignores it (each company has its own private API).
        targets = [(ats, {})]
    else:
        csv_path = DATA_ROOT / cfg["csv"]
        if not csv_path.exists():
            print(f"[{ats}] No tenant CSV at {csv_path}; nothing to scrape.")
            return 0
        with csv_path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        kwargs_factory = cfg.get("kwargs")
        for r in rows:
            slug = cfg["slug"](r)
            if slug:
                kw = kwargs_factory(r) if kwargs_factory else {}
                targets.append((slug, kw))

    if max_tenants:
        targets = targets[:max_tenants]

    print(f"[{ats}] Scraping {len(targets)} tenants (concurrency={concurrency}, timeout={timeout}s)")
    sem = asyncio.Semaphore(concurrency)
    counts = {"success": 0, "not_found": 0, "error": 0, "jobs": 0}

    output_path = DATA_ROOT / cfg["output"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg.get("publish_previous_while_running"):
        tmp_output_path = output_path.with_name(
            f".{output_path.name}.{os.getpid()}.active.tmp"
        )
    else:
        tmp_output_path = output_path.with_name(f".{output_path.name}.tmp")
    uses_streaming = bool(cfg.get("singleton") and hasattr(cfg["scraper"], "fetch_stream"))
    cache_cap = cfg.get("skip_description_cache_if_max_len_lte")
    persistent_path_rel = cfg.get("description_cache_path")
    persistent_path = (DATA_ROOT / persistent_path_rel) if persistent_path_rel else None
    cache_compress = bool(cfg.get("description_cache_compress"))
    if isinstance(cache_cap, int) and _descriptions_look_capped(output_path, cache_cap):
        print(
            f"[{ats}] Skipping previous description cache because "
            f"descriptions look capped at <= {cache_cap} chars"
        )
        description_cache = DescriptionCache(compress=cache_compress)
    elif persistent_path is not None:
        # Persistent cache: open the file and reuse it across runs. Seed from
        # the previous jobs.csv only if the cache is empty (first-run bootstrap).
        description_cache = _load_description_cache(
            output_path,
            persistent_path=persistent_path,
            compress=cache_compress,
            bootstrap_csv=output_path,
        )
    else:
        description_cache = _load_description_cache(output_path, compress=cache_compress)
    if description_cache.count:
        location = "persistent" if persistent_path else "ephemeral"
        print(
            f"[{ats}] Loaded {description_cache.count:,} cached description keys "
            f"({location} cache at {description_cache.path})"
        )
    seen_keys: set[tuple[str, str]] = set()  # (company, ats_id) for cross-tenant dedup

    t0 = time.time()
    try:
        with tmp_output_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=JOB_CSV_FIELDS)
            writer.writeheader()

            # ---- Streaming path: scrapers that would otherwise load >5 GB
            # of Job objects into RAM expose a ``fetch_stream()`` async
            # generator. We iterate it directly and write each job to disk
            # as it arrives, keeping the in-flight footprint flat (~200 MB
            # for the seen-set + a bounded asyncio.Queue) instead of
            # accumulating the full corpus. EURES is the only such ATS
            # today — its ~2.7 M-row pan-EU catalog would peak at ~10 GB
            # RSS in legacy mode, exceeding the VPS RAM budget.
            if uses_streaming:
                scraper = cfg["scraper"](ats, timeout=timeout)
                pending_descriptions: set[asyncio.Task[Job]] = set()

                def write_streamed_job(job: Job) -> None:
                    writer.writerow(_job_to_row(job))
                    counts["jobs"] += 1
                    # Periodic flush so the file is consultable while
                    # the long-running scrape is still in flight.
                    if counts["jobs"] % 10_000 == 0:
                        f.flush()
                        elapsed = time.time() - t0
                        print(
                            f"  [{ats}] streamed {counts['jobs']:,} jobs in "
                            f"{elapsed:.0f}s",
                        )

                async def enrich_missing_stream_description(job: Job) -> Job:
                    await _ensure_description(scraper, job, description_cache)
                    return job

                async def drain_description_tasks(*, all_tasks: bool = False) -> None:
                    nonlocal pending_descriptions
                    if not pending_descriptions:
                        return
                    done, pending_descriptions = await asyncio.wait(
                        pending_descriptions,
                        return_when=(
                            asyncio.ALL_COMPLETED
                            if all_tasks
                            else asyncio.FIRST_COMPLETED
                        ),
                    )
                    for task in done:
                        write_streamed_job(task.result())

                try:
                    async for job in scraper.fetch_stream():
                        cached = _cached_description(job, description_cache)
                        if cached:
                            job.description = cached
                            write_streamed_job(job)
                        elif job.description:
                            write_streamed_job(job)
                        else:
                            pending_descriptions.add(
                                asyncio.create_task(
                                    enrich_missing_stream_description(job)
                                )
                            )
                            if (
                                len(pending_descriptions)
                                >= STREAM_DESCRIPTION_CONCURRENCY
                            ):
                                await drain_description_tasks()
                    await drain_description_tasks(all_tasks=True)
                    counts["success"] = 1
                except CompanyNotFoundError:
                    for task in pending_descriptions:
                        task.cancel()
                    counts["not_found"] = 1
                except Exception as exc:
                    for task in pending_descriptions:
                        task.cancel()
                    counts["error"] = 1
                    print(f"  [{ats}] streaming failed: {type(exc).__name__}: "
                          f"{str(exc)[:200]}")
            else:
                async def task(slug: str, kw: dict[str, Any]) -> None:
                    started = time.time()
                    async with sem:
                        _, scraper, jobs, err = await _run_scraper(
                            cfg["scraper"],
                            slug,
                            kw,
                            timeout,
                            include_descriptions=not bool(
                                cfg.get("defer_descriptions_to_cache")
                            ),
                        )
                    if err == "not_found":
                        counts["not_found"] += 1
                        return
                    if err:
                        counts["error"] += 1
                        elapsed = time.time() - started
                        print(
                            f"  [{ats}] tenant failed after {elapsed:.0f}s: "
                            f"{slug} ({err})"
                        )
                        return
                    counts["success"] += 1
                    desc_stats = {
                        "cache": 0,
                        "present": 0,
                        "fetched": 0,
                        "missing": 0,
                        "error": 0,
                    }
                    for job in jobs:
                        key = (job.company, job.ats_id)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        if scraper is not None and not cfg.get("skip_description_enrichment"):
                            desc_status = await _ensure_description(
                                scraper, job, description_cache
                            )
                            desc_stats[desc_status] += 1
                        writer.writerow(_job_to_row(job))
                        counts["jobs"] += 1
                    elapsed = time.time() - started
                    if elapsed >= float(cfg.get("slow_tenant_log_seconds", 300)):
                        print(
                            f"  [{ats}] slow tenant {slug}: {elapsed:.0f}s, "
                            f"{len(jobs):,} jobs, descriptions "
                            f"cache={desc_stats['cache']:,} "
                            f"present={desc_stats['present']:,} "
                            f"fetched={desc_stats['fetched']:,} "
                            f"missing={desc_stats['missing']:,} "
                            f"errors={desc_stats['error']:,}"
                        )

                # Process tasks in batches and flush periodically
                batch_size = 50
                for i in range(0, len(targets), batch_size):
                    batch = targets[i:i + batch_size]
                    await asyncio.gather(*(task(s, kw) for s, kw in batch))
                    f.flush()
                    elapsed = time.time() - t0
                    print(
                        f"  [{ats}] processed {min(i + batch_size, len(targets))}/{len(targets)} "
                        f"tenants in {elapsed:.0f}s — "
                        f"{counts['success']} OK, {counts['not_found']} not-found, "
                        f"{counts['error']} errors, {counts['jobs']:,} jobs"
                    )

        elapsed = time.time() - t0
        print(
            f"[{ats}] Done in {elapsed:.0f}s: {counts['jobs']:,} jobs from "
            f"{counts['success']}/{len(targets)} tenants → {output_path}"
        )

        failure_threshold = max(1, (len(targets) + 1) // 2)
        catastrophic_failure = (
            bool(targets)
            and counts["jobs"] == 0
            and counts["error"] >= failure_threshold
        )
        if catastrophic_failure:
            tmp_output_path.unlink(missing_ok=True)
            if output_path.exists():
                print(
                    f"[{ats}] ACTION keep_previous: scrape produced 0 jobs with "
                    f"{counts['error']}/{len(targets)} tenant errors; preserved "
                    f"{output_path} for the next publish."
                )
            else:
                print(
                    f"[{ats}] ACTION retry: scrape produced 0 jobs with "
                    f"{counts['error']}/{len(targets)} tenant errors and no "
                    "previous jobs.csv exists."
                )
            return 1

        tmp_output_path.replace(output_path)

        # Post-scrape description normalization. Most scrapers now emit
        # HTML in the description column (lever, ashby, greenhouse, the
        # batch of 6 that used to strip-and-flatten, apple's assembled
        # body, etc.) and rely on this step to produce clean markdown
        # before publish. Skippable per-ATS via ``skip_normalize`` for
        # paths that handle their own markdown upstream (e.g. workable
        # which fetches .md natively). Failure is non-fatal — a broken
        # normalize pass shouldn't lose the day's scrape.
        if not cfg.get("skip_normalize"):
            try:
                _normalize_output(ats, output_path)
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"[{ats}] WARN: description normalization failed "
                    f"({type(exc).__name__}: {str(exc)[:200]}); "
                    f"leaving raw scraper output in {output_path}"
                )

        if counts["error"] >= failure_threshold:
            print(
                f"[{ats}] ACTION investigate: scrape kept partial data but "
                f"{counts['error']}/{len(targets)} tenants failed."
            )
            return 1
        return 0
    finally:
        description_cache.close()


def _normalize_output(ats: str, jobs_csv: Path) -> None:
    """Invoke scripts/normalize_descriptions.py on the freshly-written
    jobs.csv. The normalize script is a separate file rather than an
    in-process import so a crash in the normalizer can't take down the
    scraper pipeline.
    """
    script_path = Path(__file__).resolve().parent / "normalize_descriptions.py"
    if not script_path.exists():
        print(f"[{ats}] WARN: {script_path} missing, skipping normalize")
        return
    workers = max(1, (os.cpu_count() or 4) - 1)
    cmd = [
        sys.executable,
        str(script_path),
        str(jobs_csv),
        "-j", str(workers),
    ]
    t0 = time.time()
    print(f"[{ats}] normalizing descriptions ({workers} workers)...")
    # Upper-bound the normalize step so a hung worker can't wedge the
    # whole daily pipeline. EURES (~2.7M rows) is the largest single
    # CSV we feed in; even at ~500 rows/sec/worker that's ~30 min, so
    # 2 h leaves a generous margin. ``JOBHIVE_NORMALIZE_TIMEOUT_S``
    # tunes it for ATSes that grow past that envelope.
    timeout_s = int(os.environ.get("JOBHIVE_NORMALIZE_TIMEOUT_S", "7200"))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        print(
            f"[{ats}] WARN: normalize exceeded {timeout_s}s budget "
            f"(elapsed={elapsed:.0f}s); jobs.csv left in its pre-normalize state."
        )
        return
    if result.returncode != 0:
        print(
            f"[{ats}] WARN: normalize exited {result.returncode}; "
            f"stderr tail: {result.stderr[-400:]}"
        )
        return
    elapsed = time.time() - t0
    last_line = (result.stdout or "").strip().splitlines()[-1:] or [""]
    print(f"[{ats}] normalize done in {elapsed:.1f}s — {last_line[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ats", choices=sorted(CONFIGS.keys()))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-tenants", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    with _pipeline_lock(args.ats) as acquired:
        if not acquired:
            return 0
        return asyncio.run(run(args.ats, args.concurrency, args.max_tenants, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
