from __future__ import annotations

import csv
from pathlib import Path


def test_pandape_catalog_contains_only_canonical_career_pages() -> None:
    with Path("ats-companies/pandape.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 3_000
    assert all(row["slug"] == row["slug"].strip().lower() != "" for row in rows)
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(
        row["url"] == f"https://{row['slug']}.pandape.infojobs.com.br" for row in rows
    )
    assert not {"www", "empregos"} & {row["slug"] for row in rows}
