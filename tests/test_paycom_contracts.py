from __future__ import annotations

import csv
from pathlib import Path


def test_paycom_catalog_contains_only_validated_canonical_portals() -> None:
    with Path("ats-companies/paycom.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 5_135
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"]
        == (
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            f"{row['slug']}/career-page"
        )
        for row in rows
    )
