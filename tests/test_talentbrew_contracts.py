from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.talentbrew import TalentBrewScraper, _normalize_host
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency, _tolerated_shard_failures


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/talentbrew.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_talentbrew_pipeline_config() -> None:
    config = CONFIGS["talentbrew"]
    row = {"name": "VINCI", "slug": "jobs.vinci.com", "url": "https://jobs.vinci.com"}

    assert config["scraper"] is TalentBrewScraper
    assert config["slug"](row) == "https://jobs.vinci.com"
    assert config["kwargs"](row) == {"company_name": "VINCI"}
    assert config["csv"] == "ats-companies/talentbrew.csv"
    assert config["output"] == "talentbrew/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    # Posting date, country and employment type only exist on the detail
    # page, so details are read on every run instead of deferring
    # descriptions to the cache (which would keep the description only).
    assert "defer_descriptions_to_cache" not in config
    assert _bounded_concurrency(config, 24) == 4


def test_talentbrew_fails_closed_on_errors_missing_sites_and_empty_output() -> None:
    config = CONFIGS["talentbrew"]

    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True


def test_talentbrew_tolerance_covers_one_site_in_ten() -> None:
    config = CONFIGS["talentbrew"]
    targets = len(_rows())
    tolerated = _tolerated_shard_failures(config, targets)

    assert config["fail_closed_tolerance"] == 0.1
    # One site being down does not hold back every other employer...
    assert tolerated >= 1
    # ...but the guard still trips well before a real outage.
    assert tolerated <= targets // 10


def test_talentbrew_is_a_direct_employer_source_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["talentbrew"] == ATS_DEDUP_PRIORITY["workday"] == 1


def test_talentbrew_catalogue_is_one_https_origin_per_employer() -> None:
    rows = _rows()

    assert len(rows) >= 19
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["name"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    for row in rows:
        assert row["slug"] == _normalize_host(row["url"])
        assert row["url"] == f"https://{row['slug']}"
