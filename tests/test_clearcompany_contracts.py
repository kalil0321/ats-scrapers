from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.clearcompany import _parse_slug

MIN_ROWS = 508


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/clearcompany.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_clearcompany_catalogue_is_one_row_per_career_site() -> None:
    rows = _rows()

    assert len(rows) >= MIN_ROWS
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    names = [row["name"].casefold() for row in rows]
    assert names == sorted(names)
    for row in rows:
        assert _parse_slug(row["slug"]) == row["slug"]
        assert row["url"] == f"https://apply.clearcompany.com/{row['slug']}"
        # The page title, not the title with its suffix.
        assert not row["name"].endswith(" – Careers")
