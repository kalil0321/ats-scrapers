from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.catsone import _parse_slug

MIN_ROWS = 289


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/catsone.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


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
