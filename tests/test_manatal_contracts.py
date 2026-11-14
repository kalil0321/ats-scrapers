from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import ManatalScraper
from ats_scrapers.source_kind import source_kind
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency

EXPECTED_ROWS = 1563


def test_manatal_pipeline_config() -> None:
    config = CONFIGS["manatal"]
    row = {
        "name": "Acme Search",
        "slug": "acme-search",
        "url": "https://www.careers-page.com/acme-search",
    }

    assert config["scraper"] is ManatalScraper
    assert config["slug"](row) == "acme-search"
    assert config["kwargs"](row) == {"company_name": "Acme Search"}
    assert config["csv"] == "ats-companies/manatal.csv"
    assert config["output"] == "manatal/jobs.csv"
    assert _bounded_concurrency(config, 24) == 1
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.05


def test_manatal_is_an_employer_ats() -> None:
    assert source_kind("manatal") == "ats"
    assert ATS_DEDUP_PRIORITY["manatal"] == ATS_DEDUP_PRIORITY["workday"]


def test_manatal_catalogue_is_well_formed() -> None:
    with Path("ats-companies/manatal.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    slugs = [row["slug"] for row in rows]
    assert len(set(slugs)) == len(slugs)
    assert slugs == sorted(slugs, key=lambda slug: (slug.casefold(), slug))
    for row in rows:
        assert row["name"].strip()
        assert row["url"] == f"https://www.careers-page.com/{row['slug']}"
        assert ManatalScraper(row["slug"]).company_slug == row["slug"]
