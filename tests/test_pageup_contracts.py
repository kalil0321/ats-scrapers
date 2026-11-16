"""Pipeline contracts for PageUp."""

from __future__ import annotations

import csv
from pathlib import Path


def test_pageup_seed_catalogue_is_provider_only() -> None:
    with Path("ats-companies/pageup.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) > 1
    assert all(
        row["url"].startswith("https://careers.pageuppeople.com/")
        for row in rows
    )
    assert len({row["slug"] for row in rows}) == len(rows)
