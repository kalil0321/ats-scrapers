from __future__ import annotations

import csv
from pathlib import Path


def test_isolved_hire_catalog_contains_only_canonical_portals() -> None:
    with Path("ats-companies/isolved_hire.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 296
    assert all(row["slug"].strip() for row in rows)
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.isolvedhire.com/jobs/" for row in rows
    )
