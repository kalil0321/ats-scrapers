from __future__ import annotations

import csv
from pathlib import Path


def test_keka_catalog_contains_only_validated_nonempty_portals() -> None:
    with Path("ats-companies/keka.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 392
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"].startswith("https://")
        and ".keka.com/careers" in row["url"]
        for row in rows
    )
