from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import quote

from ats_scrapers.scrapers import HireologyScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_hireology_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["hireology"]
    row = {
        "name": "Anderson Auto Group",
        "slug": "andersonautogroup",
        "url": "https://careers.hireology.com/andersonautogroup/",
    }

    assert config["scraper"] is HireologyScraper
    assert config["slug"](row) == "andersonautogroup"
    assert config["kwargs"](row) == {
        "company_name": "Anderson Auto Group",
    }
    assert config["csv"] == "ats-companies/hireology.csv"
    assert config["output"] == "hireology/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 8
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_on_any_error"] is True
    assert "defer_descriptions_to_cache" not in config


def test_hireology_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["hireology"], 24) == 8


def test_hireology_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["hireology"] == ATS_DEDUP_PRIORITY["workday"]


def test_hireology_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/hireology.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 4_494
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == (
            "https://careers.hireology.com/"
            f"{quote(row['slug'], safe='-._~')}/"
        )
        for row in rows
    )
