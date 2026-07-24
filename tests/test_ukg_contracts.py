"""Pipeline contracts for UKG Pro Recruiting."""

from __future__ import annotations

from pathlib import Path

from ats_scrapers.scrapers.ukg import UKGProScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_ukg_pipeline_contract() -> None:
    config = CONFIGS["ukg"]

    assert config["scraper"] is UKGProScraper
    assert config["csv"] == "ats-companies/ukg.csv"
    assert config["output"] == "ukg/jobs.csv"
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["kwargs"]({"name": "Acme"}) == {"company_name": "Acme"}


def test_ukg_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["ukg"] == ATS_DEDUP_PRIORITY["workday"]


def test_ukg_seed_catalogue_is_provider_only() -> None:
    rows = Path("ats-companies/ukg.csv").read_text().splitlines()

    assert len(rows) == 20
    assert all("ultipro." in row for row in rows[1:])
