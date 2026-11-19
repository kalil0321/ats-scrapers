from __future__ import annotations

import csv
from pathlib import Path


def test_careerplug_catalog_contains_only_canonical_sites() -> None:
    with Path("ats-companies/careerplug.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 100
    assert all(row["slug"] == row["slug"].strip().lower() != "" for row in rows)
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.careerplug.com/jobs" for row in rows
    )
