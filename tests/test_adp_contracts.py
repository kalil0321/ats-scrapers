"""Production integration contracts for ADP Workforce Now."""

from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ats_scrapers.scrapers.adp import ADPWorkforceNowScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_adp_pipeline_contract() -> None:
    config = CONFIGS["adp"]

    assert config["scraper"] is ADPWorkforceNowScraper
    assert config["csv"] == "ats-companies/adp.csv"
    assert config["output"] == "adp/jobs.csv"
    assert config["defer_descriptions_to_cache"] is True
    assert config["description_cache_path"] == "adp/descriptions.sqlite3"
    assert config["description_cache_compress"] is True
    assert config["max_concurrency"] == 1
    assert config["tenant_delay_seconds"] == 0.5
    assert config["description_concurrency"] == 1
    assert config["description_delay_seconds"] == 0.5
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["kwargs"]({"name": "Acme"}) == {"company_name": "Acme"}


def test_adp_has_direct_employer_dedup_priority() -> None:
    assert ATS_DEDUP_PRIORITY["adp"] == ATS_DEDUP_PRIORITY["workday"]


def test_adp_seed_catalogue_is_provider_only() -> None:
    with Path("ats-companies/adp.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert len({row["url"].casefold() for row in rows}) == len(rows)
    assert all("/" in row["slug"] for row in rows)
    assert all(
        row["url"].startswith(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
        )
        for row in rows
    )
    for row in rows:
        cid, career_center_id = row["slug"].split("/", 1)
        query = parse_qs(urlparse(row["url"]).query)

        assert query["cid"] == [cid]
        assert query["ccId"] == [career_center_id]
        assert query["lang"][0]
        assert row["name"]
        assert row["name"].casefold() not in {"facebook", "linkedin"}
        assert not row["name"].casefold().endswith((".jpg", ".jpeg", ".png"))
