from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import KekaScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_keka_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["keka"]
    row = {
        "name": "Bright Future",
        "url": "https://100.keka.com/careers",
    }

    assert config["scraper"] is KekaScraper
    assert config["slug"](row) == row["url"]
    assert config["kwargs"](row) == {"company_name": "Bright Future"}
    assert config["csv"] == "ats-companies/keka.csv"
    assert config["output"] == "keka/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 8
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert "defer_descriptions_to_cache" not in config


def test_keka_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["keka"], 24) == 8


def test_keka_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["keka"] == ATS_DEDUP_PRIORITY["workday"]


def test_keka_catalog_contains_only_validated_nonempty_portals() -> None:
    with Path("ats-companies/keka.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 185
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"].startswith("https://")
        and ".keka.com/careers" in row["url"]
        for row in rows
    )
