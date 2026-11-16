from __future__ import annotations

import csv
from pathlib import Path


def test_applicantpro_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/applicantpro.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 1_904
    assert all(row["slug"].strip() for row in rows)
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.applicantpro.com/jobs/"
        for row in rows
    )
