from __future__ import annotations

import csv
from pathlib import Path


def test_applitrack_catalog_rows_are_distinct_board_urls() -> None:
    with Path("ats-companies/applitrack.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2_113
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["country_iso"] in {"", "CA", "US"} for row in rows)
    assert all(row["slug"] == row["url"] for row in rows)
    assert all(
        row["url"].startswith("https://www.applitrack.com/")
        and row["url"].endswith("/onlineapp")
        for row in rows
    )
