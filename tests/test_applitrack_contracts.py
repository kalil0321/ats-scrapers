from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import AppliTrackScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import (
    CONFIGS,
    _bounded_concurrency,
    _bounded_timeout,
)


def test_applitrack_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["applitrack"]
    row = {
        "name": "Leander Independent School District",
        "slug": "https://www.applitrack.com/leander/onlineapp",
        "url": "https://www.applitrack.com/leander/onlineapp",
        "country_iso": "US",
    }

    assert config["scraper"] is AppliTrackScraper
    assert config["slug"](row) == row["slug"]
    assert config["kwargs"](row) == {
        "company_name": "Leander Independent School District",
        "country_iso": "US",
    }
    assert config["csv"] == "ats-companies/applitrack.csv"
    assert config["output"] == "applitrack/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert "dedupe_by_content" not in config
    assert config["max_concurrency"] == 4
    assert config["min_timeout"] == 120.0
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert "defer_descriptions_to_cache" not in config


def test_applitrack_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["applitrack"], 24) == 4
    assert _bounded_timeout(CONFIGS["applitrack"], 30.0) == 120.0


def test_applitrack_is_a_direct_employer_ats_for_deduplication() -> None:
    assert (
        ATS_DEDUP_PRIORITY["applitrack"]
        == ATS_DEDUP_PRIORITY["workday"]
    )


def test_applitrack_catalog_contains_only_live_nonempty_tenants() -> None:
    with Path("ats-companies/applitrack.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2_149
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["country_iso"] in {"", "CA", "US"} for row in rows)
    assert all(row["slug"] == row["url"] for row in rows)
    assert all(
        row["url"].startswith("https://www.applitrack.com/")
        and row["url"].endswith("/onlineapp")
        for row in rows
    )
