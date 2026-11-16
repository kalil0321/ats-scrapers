from __future__ import annotations

import csv
from pathlib import Path


def test_hrmos_catalog_contains_only_validated_nonempty_portals() -> None:
    with Path("ats-companies/hrmos.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 1_544
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://hrmos.co/pages/{row['slug']}/jobs"
        for row in rows
    )
