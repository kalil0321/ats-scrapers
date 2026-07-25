"""Pipeline contracts for PageUp."""

from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.pageup import PageUpScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_pageup_pipeline_contract() -> None:
    config = CONFIGS["pageup"]

    assert config["scraper"] is PageUpScraper
    assert config["csv"] == "ats-companies/pageup.csv"
    assert config["output"] == "pageup/jobs.csv"
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["kwargs"]({"name": "Monash"}) == {
        "company_name": "Monash"
    }


def test_pageup_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["pageup"] == ATS_DEDUP_PRIORITY["workday"]


def test_pageup_seed_catalogue_is_provider_only() -> None:
    with Path("ats-companies/pageup.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) > 1
    assert all(
        row["url"].startswith("https://careers.pageuppeople.com/")
        for row in rows
    )
    assert len({row["slug"] for row in rows}) == len(rows)
