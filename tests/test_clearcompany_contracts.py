from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.clearcompany import ClearCompanyScraper, _parse_slug
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures

MIN_ROWS = 514


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/clearcompany.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_clearcompany_pipeline_config() -> None:
    config = CONFIGS["clearcompany"]
    row = {
        "name": "AVI Foodsystems",
        "slug": "avifoodsystems",
        "url": "https://apply.clearcompany.com/avifoodsystems",
    }

    assert config["scraper"] is ClearCompanyScraper
    assert config["slug"](row) == "avifoodsystems"
    assert config["kwargs"](row) == {"company_name": "AVI Foodsystems"}
    assert config["csv"] == "ats-companies/clearcompany.csv"
    assert config["output"] == "clearcompany/jobs.csv"
    # A posting listed on several career sites of one customer is
    # published once.
    assert config["dedupe_by_ats_id"] is True
    assert _bounded_concurrency(config, 24) == 8


def test_clearcompany_slug_falls_back_to_the_url_column() -> None:
    config = CONFIGS["clearcompany"]
    row = {"name": "Acme", "slug": "", "url": "https://apply.clearcompany.com/acme"}

    assert _parse_slug(config["slug"](row)) == "acme"


def test_clearcompany_fails_closed_with_a_small_tolerance() -> None:
    config = CONFIGS["clearcompany"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.05
    # A few sites being down or gone does not hold back the others...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 20


def test_clearcompany_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["clearcompany"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_clearcompany_catalogue_is_one_row_per_career_site() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    names = [row["name"].casefold() for row in rows]
    assert names == sorted(names)
    for row in rows:
        assert _parse_slug(row["slug"]) == row["slug"]
        assert row["url"] == f"https://apply.clearcompany.com/{row['slug']}"
        # The page title, not the title with its suffix.
        assert not row["name"].endswith("Careers")
