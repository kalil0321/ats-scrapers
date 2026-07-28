"""Pipeline contracts for Paylocity."""

from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.paylocity import PaylocityScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_paylocity_pipeline_contract() -> None:
    config = CONFIGS["paylocity"]

    assert config["scraper"] is PaylocityScraper
    assert config["csv"] == "ats-companies/paylocity.csv"
    assert config["output"] == "paylocity/jobs.csv"
    assert config["max_concurrency"] == 1
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["kwargs"]({"name": "Acme"}) == {"company_name": "Acme"}


def test_provider_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["paylocity"], 8) == 1
    assert _bounded_concurrency({}, 8) == 8


def test_paylocity_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["paylocity"] == ATS_DEDUP_PRIORITY["workday"]


def test_paylocity_seed_catalogue_is_validated_provider_only() -> None:
    with Path("ats-companies/paylocity.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 48
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(
        row["url"]
        == (
            "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
            + row["slug"]
        )
        for row in rows
    )
