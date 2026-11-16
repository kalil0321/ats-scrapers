from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import WantedlyScraper

# Companies that listed at least one project when the catalogue was built.
# A catalogue cut short by accident fails here.
EXPECTED_ROWS = 3443


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
