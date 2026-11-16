from __future__ import annotations

import csv
from pathlib import Path


def test_softgarden_catalog_contains_only_validated_canonical_feeds() -> None:
    with Path("ats-companies/softgarden.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 912
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == f"https://{row['slug']}.career.softgarden.de/"
        for row in rows
    )
