from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import ApplicantProScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_applicantpro_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["applicantpro"]
    row = {
        "name": "Kirkhill Inc.",
        "slug": "kirkhill",
        "url": "https://kirkhill.applicantpro.com/jobs/",
    }

    assert config["scraper"] is ApplicantProScraper
    assert config["slug"](row) == "kirkhill"
    assert config["kwargs"](row) == {"company_name": "Kirkhill Inc."}
    assert config["csv"] == "ats-companies/applicantpro.csv"
    assert config["output"] == "applicantpro/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 4
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert "defer_descriptions_to_cache" not in config


def test_applicantpro_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["applicantpro"], 12) == 4


def test_applicantpro_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["applicantpro"] == ATS_DEDUP_PRIORITY["workday"]


def test_applicantpro_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/applicantpro.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1_956
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.applicantpro.com/jobs/"
        for row in rows
    )
