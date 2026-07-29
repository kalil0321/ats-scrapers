from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import HrmosScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_hrmos_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["hrmos"]
    row = {
        "name": "ＹＫＫ株式会社",
        "slug": "ykk",
        "url": "https://hrmos.co/pages/ykk/jobs",
    }

    assert config["scraper"] is HrmosScraper
    assert config["slug"](row) == "ykk"
    assert config["csv"] == "ats-companies/hrmos.csv"
    assert config["output"] == "hrmos/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 6
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_on_any_error"] is True
    assert "defer_descriptions_to_cache" not in config


def test_hrmos_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["hrmos"], 24) == 6


def test_hrmos_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["hrmos"] == ATS_DEDUP_PRIORITY["workday"]


def test_hrmos_catalog_contains_only_validated_nonempty_portals() -> None:
    with Path("ats-companies/hrmos.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 960
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://hrmos.co/pages/{row['slug']}/jobs"
        for row in rows
    )
