"""Production integration contracts for Dayforce."""

from __future__ import annotations

import csv
from pathlib import Path


def test_dayforce_seed_has_unique_validated_feeds() -> None:
    path = Path("ats-companies/dayforce.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 1_541
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert all("/" in row["slug"] for row in rows)
    assert all(
        row["url"].startswith("https://jobs.dayforcehcm.com/")
        for row in rows
    )
