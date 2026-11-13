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
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.02


def test_talentio_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["talentio"], 24) == 2


def test_talentio_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["talentio"] == ATS_DEDUP_PRIORITY["workday"]


def test_talentio_catalog_holds_the_validated_homes() -> None:
    with Path("ats-companies/talentio.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    # 633 homes minus the 17 that answered 404 twice on 13 November 2026.
    # A catalogue cut short by accident fails here.
    assert len(rows) == 616
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    # Every row passes the scraper's own URL check and is stored in the form
    # the scraper requests, so no row can reach the pipeline and be refused.
    for row in rows:
        assert TalentioScraper(row["url"]).portal_url == row["url"]
