from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.catsone import CatsOneScraper, _parse_slug
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures

MIN_ROWS = 289


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/catsone.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_catsone_pipeline_config() -> None:
    config = CONFIGS["catsone"]
    row = {
        "name": "emergiTEL Inc.",
        "slug": "emergitel",
        "url": "https://emergitel.catsone.com/careers/",
    }

    assert config["scraper"] is CatsOneScraper
    assert config["slug"](row) == "emergitel"
    assert config["kwargs"](row) == {"company_name": "emergiTEL Inc."}
    assert config["csv"] == "ats-companies/catsone.csv"
    assert config["output"] == "catsone/jobs.csv"
    # A job has one number on every portal: published once.
    assert config["dedupe_by_ats_id"] is True
    assert _bounded_concurrency(config, 24) == 6


def test_catsone_slug_falls_back_to_the_url_column() -> None:
    config = CONFIGS["catsone"]
    row = {"name": "Acme", "slug": "", "url": "https://acme.catsone.com/careers/"}

    assert _parse_slug(config["slug"](row)) == ("acme", None)


def test_catsone_fails_closed_with_a_small_tolerance() -> None:
    config = CONFIGS["catsone"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_on_any_error"] is True
    # Portals that are gone count against the tolerance too.
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.05
    # A few portals being down or gone does not hold back the others...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 20


def test_catsone_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["catsone"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_catsone_catalogue_is_one_row_per_portal() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"] for row in rows}) == len(rows)
    order = [(row["name"].casefold(), row["slug"]) for row in rows]
    assert order == sorted(order)
    for row in rows:
        assert row["name"].strip() == row["name"] != ""
        assert _parse_slug(row["slug"]) == (row["slug"], None)
        assert row["url"] == f"https://{row['slug']}.catsone.com/careers/"
