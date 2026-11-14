from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import WantedlyScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency

# Companies that listed at least one project when the catalogue was built.
# A catalogue cut short by accident fails here.
EXPECTED_ROWS = 1639


def test_wantedly_pipeline_reads_the_company_catalogue() -> None:
    config = CONFIGS["wantedly"]
    row = {
        "name": "Wantedly, Inc.",
        "slug": "wantedly",
        "url": "https://www.wantedly.com/companies/wantedly",
    }

    assert config["scraper"] is WantedlyScraper
    assert config["slug"](row) == "wantedly"
    assert config["kwargs"](row) == {"company_name": "Wantedly, Inc."}
    assert config["csv"] == "ats-companies/wantedly.csv"
    assert config["output"] == "wantedly/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    # The site answers 403 to a faster client.
    assert config["max_concurrency"] == 2
    assert _bounded_concurrency(config, 24) == 2
    assert config["fail_closed_on_any_error"] is True
    assert config["fail_closed_on_not_found"] is True
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_tolerance"] == 0.02


def test_wantedly_ranks_with_the_boards_where_employers_post() -> None:
    assert ATS_DEDUP_PRIORITY["wantedly"] == ATS_DEDUP_PRIORITY["welcometothejungle"]


def test_wantedly_is_a_job_board_outside_the_headline_count() -> None:
    """Employers post on Wantedly's marketplace; it is not their own hiring
    system, so its projects do not count toward the headline total."""
    from ats_scrapers.source_kind import is_headline, source_kind

    assert source_kind("wantedly") == "job_board"
    assert is_headline("wantedly") is False


def test_wantedly_catalogue_rows_are_companies_the_scraper_accepts() -> None:
    with Path("ats-companies/wantedly.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    slugs = [row["slug"] for row in rows]
    assert len(set(slugs)) == len(slugs)
    assert slugs == sorted(slugs, key=lambda slug: (slug.casefold(), slug))
    for row in rows:
        assert row["name"].strip()
        assert WantedlyScraper(row["slug"]).company_slug == row["slug"]
        assert row["url"] == f"https://www.wantedly.com/companies/{row['slug']}"
