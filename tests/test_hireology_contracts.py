from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import quote


def test_hireology_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/hireology.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 8_555
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == (
            "https://careers.hireology.com/"
            f"{quote(row['slug'], safe='-._~')}/"
        )
        for row in rows
    )
