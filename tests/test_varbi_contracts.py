from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import VarbiScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_varbi_pipeline_contract() -> None:
    config = CONFIGS["varbi"]
    assert config["scraper"] is VarbiScraper
    assert config["csv"] == "ats-companies/varbi.csv"
    assert config["output"] == "varbi/jobs.csv"
    assert config["kwargs"]({"name": "Acme"}) == {"company_name": "Acme"}
    assert _bounded_concurrency(config, 8) == 2
    assert config["dedupe_by_ats_id"] is True
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert ATS_DEDUP_PRIORITY["varbi"] == ATS_DEDUP_PRIORITY["workday"]


def test_varbi_catalog_uses_unique_public_employer_boards() -> None:
    with Path("ats-companies/varbi.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["url"] == f"https://{row['slug']}.varbi.com/" for row in rows)
    assert all(row["slug"] not in {"www", "demo", "test"} for row in rows)
