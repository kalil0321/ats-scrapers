from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import NinehireScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_ninehire_pipeline_is_fail_closed() -> None:
    config = CONFIGS["ninehire"]
    row = {
        "name": "데이원컴퍼니",
        "slug": "day1company",
        "url": "https://day1company.ninehire.site/",
    }

    assert config["scraper"] is NinehireScraper
    assert config["slug"](row) == "day1company"
    assert config["kwargs"](row) == {"company_name": "데이원컴퍼니"}
    assert config["csv"] == "ats-companies/ninehire.csv"
    assert config["output"] == "ninehire/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 6
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_on_any_error"] is True


def test_ninehire_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["ninehire"], 24) == 6


def test_ninehire_is_a_direct_employer_ats() -> None:
    assert ATS_DEDUP_PRIORITY["ninehire"] == ATS_DEDUP_PRIORITY["workday"]


def test_ninehire_catalog_contains_only_selected_direct_employers() -> None:
    with Path("ats-companies/ninehire.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 29
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.ninehire.site/"
        for row in rows
    )

    slugs = {row["slug"] for row in rows}
    assert "ezrecruit" not in slugs
    assert "aknac1" not in slugs
    assert "classting" not in slugs
    assert "dealmakers" not in slugs
    assert "ddv6dme7" not in slugs
