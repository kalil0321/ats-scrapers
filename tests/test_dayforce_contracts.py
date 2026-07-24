"""Production integration contracts for Dayforce."""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_dayforce_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["dayforce"] == ATS_DEDUP_PRIORITY["workday"]


def test_dayforce_pipeline_fails_closed() -> None:
    config = CONFIGS["dayforce"]
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["csv"] == "ats-companies/dayforce.csv"
    assert config["output"] == "dayforce/jobs.csv"


def test_dayforce_seed_has_unique_validated_feeds() -> None:
    path = Path("ats-companies/dayforce.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 300
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert all("/" in row["slug"] for row in rows)
    assert all(
        row["url"].startswith("https://jobs.dayforcehcm.com/")
        for row in rows
    )
