from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import SoftgardenScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_softgarden_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["softgarden"]
    row = {
        "name": "ABEKING & RASMUSSEN",
        "slug": "abeking",
        "url": "https://abeking.career.softgarden.de/",
    }

    assert config["scraper"] is SoftgardenScraper
    assert config["slug"](row) == "abeking"
    assert config["csv"] == "ats-companies/softgarden.csv"
    assert config["output"] == "softgarden/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 8
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_on_any_error"] is True
    assert "defer_descriptions_to_cache" not in config


def test_softgarden_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["softgarden"], 24) == 8


def test_softgarden_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["softgarden"] == ATS_DEDUP_PRIORITY["workday"]


def test_softgarden_catalog_contains_only_validated_canonical_feeds() -> None:
    with Path("ats-companies/softgarden.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 392
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == f"https://{row['slug']}.career.softgarden.de/"
        for row in rows
    )
