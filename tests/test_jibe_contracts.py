from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.jibe import JibeScraper, _normalize_host
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/jibe.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_jibe_pipeline_config() -> None:
    config = CONFIGS["jibe"]
    row = {"name": "Costco", "slug": "careers.costco.com", "url": "https://careers.costco.com"}

    assert config["scraper"] is JibeScraper
    assert config["slug"](row) == "https://careers.costco.com"
    assert config["kwargs"](row) == {"company_name": "Costco"}
    assert config["csv"] == "ats-companies/jibe.csv"
    assert config["output"] == "jibe/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert _bounded_concurrency(config, 24) == 4


def test_jibe_fails_closed_on_errors_missing_sites_and_empty_output() -> None:
    config = CONFIGS["jibe"]

    # An employer that errors, answers 404 or disappears must not silently
    # drop out of the published file.
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True


def test_jibe_tolerance_covers_a_few_employers_only() -> None:
    config = CONFIGS["jibe"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_tolerance"] == 0.02
    # One site being down does not hold back every other employer...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 50


def test_jibe_is_a_direct_employer_ats_for_deduplication() -> None:
    # Jibe is the iCIMS career site on the employer's own domain.
    assert ATS_DEDUP_PRIORITY["jibe"] == ATS_DEDUP_PRIORITY["icims"] == 1


def test_jibe_catalogue_is_one_https_origin_per_employer() -> None:
    rows = _rows()

    assert len(rows) >= 150
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    for row in rows:
        assert row["slug"] == _normalize_host(row["url"])
        assert row["url"] == f"https://{row['slug']}"
