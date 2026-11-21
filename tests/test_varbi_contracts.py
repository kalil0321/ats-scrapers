from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import VarbiScraper


def test_varbi_catalog_uses_unique_public_employer_boards() -> None:
    with Path("ats-companies/varbi.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 282
    assert len({row["slug"] for row in rows}) == len(rows)
    names = [row["name"].casefold() for row in rows]
    assert names == sorted(names)
    assert all(row["name"].strip() for row in rows)
    assert all(row["url"] == f"https://{row['slug']}.varbi.com/" for row in rows)
    assert all(row["slug"] not in {"www", "demo", "test"} for row in rows)
    # The scraper itself accepts every slug (it rejects platform hosts such
    # as ``api`` or ``login``) and reads the feed of the row's own board.
    for row in rows:
        scraper = VarbiScraper(row["slug"])
        assert scraper.feed_url == f"{row['url']}what:rssfeed/"
