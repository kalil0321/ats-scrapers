from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers import ZohoRecruitScraper

EXPECTED_ROWS = 4358
URL_RE = re.compile(
    r"https://[a-z0-9][a-z0-9-]*\.zohorecruit\.(?:com|eu|in)/jobs/[^/\s?#]+"
)


def test_zoho_recruit_catalogue_is_well_formed() -> None:
    path = Path("ats-companies/zoho_recruit.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    urls = [row["url"] for row in rows]
    assert len({url.split("/")[2] for url in urls}) == len(urls)
    assert urls == sorted(urls, key=lambda url: (url.casefold(), url))
    for row in rows:
        assert row["name"].strip()
        assert URL_RE.fullmatch(row["url"])
        assert row["slug"] == row["url"]
        assert ZohoRecruitScraper(row["slug"]).portal_url == row["url"]
