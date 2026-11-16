from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import TalentioScraper


def test_talentio_catalog_holds_the_validated_homes() -> None:
    with Path("ats-companies/talentio.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    # 633 homes minus the 17 that answered 404 twice on 13 November 2026.
    # A catalogue cut short by accident fails here.
    assert len(rows) == 616
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    # Every row passes the scraper's own URL check and is stored in the form
    # the scraper requests, so no row can reach the pipeline and be refused.
    for row in rows:
        assert TalentioScraper(row["url"]).portal_url == row["url"]
