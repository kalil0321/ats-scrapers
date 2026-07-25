"""Pipeline contracts for Jobvite."""

from __future__ import annotations

from pathlib import Path

from ats_scrapers.scrapers.jobvite import JobviteScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_jobvite_pipeline_contract() -> None:
    config = CONFIGS["jobvite"]

    assert config["scraper"] is JobviteScraper
    assert config["csv"] == "ats-companies/jobvite.csv"
    assert config["output"] == "jobvite/jobs.csv"
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["kwargs"]({"name": "Acme"}) == {"company_name": "Acme"}


def test_jobvite_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["jobvite"] == ATS_DEDUP_PRIORITY["workday"]


def test_jobvite_seed_catalogue_is_provider_only() -> None:
    rows = Path("ats-companies/jobvite.csv").read_text().splitlines()

    assert len(rows) > 1
    assert all("jobs.jobvite.com" in row for row in rows[1:])
