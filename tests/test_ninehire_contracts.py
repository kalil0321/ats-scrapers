from __future__ import annotations

import csv
from pathlib import Path


def test_ninehire_catalog_contains_only_selected_direct_employers() -> None:
    with Path("ats-companies/ninehire.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 29
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    names = [row["name"] for row in rows]
    assert names == sorted(names)
    assert all(
        row["url"] == f"https://{row['slug']}.ninehire.site/"
        for row in rows
    )

    slugs = {row["slug"] for row in rows}
    assert "ezrecruit" not in slugs
    assert "aknac1" not in slugs
    assert "classting" not in slugs
    assert "dealmakers" not in slugs
    assert "ddv6dme7" not in slugs
