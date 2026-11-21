from __future__ import annotations

import csv
from pathlib import Path


def test_onlyfy_catalog_contains_only_canonical_career_pages() -> None:
    with Path("ats-companies/onlyfy.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 500
    assert all(row["slug"] == row["slug"].strip().lower() != "" for row in rows)
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["url"] == f"https://{row['slug']}.onlyfy.jobs" for row in rows)
