from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import ManatalScraper

EXPECTED_ROWS = 1563


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
