from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import TalentioScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_talentio_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["talentio"]
    row = {
        "name": "株式会社ビューン",
        "url": "https://open.talentio.com/r/1/c/viewn/homes/2635",
    }

    assert config["scraper"] is TalentioScraper
    assert config["slug"](row) == row["url"]
    assert config["kwargs"](row) == {"company_name": "株式会社ビューン"}
    assert config["csv"] == "ats-companies/talentio.csv"
    assert config["output"] == "talentio/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 2
    assert config["tenant_delay_seconds"] == 0.25
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True


def test_talentio_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["talentio"], 24) == 2


def test_talentio_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["talentio"] == ATS_DEDUP_PRIORITY["workday"]


def test_talentio_catalog_contains_only_validated_nonempty_portals() -> None:
    with Path("ats-companies/talentio.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"].startswith(
            (
                "https://open.talentio.com/r/1/c/",
                "https://recruit.talentio.co.jp/r/1/c/",
            )
        )
        and "/homes/" in row["url"]
        for row in rows
    )
