from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import PaycomScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_paycom_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["paycom"]
    row = {
        "name": "YMCA OF THE ROCKIES",
        "slug": "00f1f305d986350f2a5df3d1ae79350f",
        "url": (
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            "00f1f305d986350f2a5df3d1ae79350f/career-page"
        ),
    }

    assert config["scraper"] is PaycomScraper
    assert config["slug"](row) == row["slug"]
    assert config["kwargs"](row) == {"company_name": "YMCA OF THE ROCKIES"}
    assert config["csv"] == "ats-companies/paycom.csv"
    assert config["output"] == "paycom/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 3
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert "defer_descriptions_to_cache" not in config


def test_paycom_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["paycom"], 12) == 3


def test_paycom_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["paycom"] == ATS_DEDUP_PRIORITY["workday"]


def test_paycom_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/paycom.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 5_135
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == (
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            f"{row['slug']}/career-page"
        )
        for row in rows
    )
