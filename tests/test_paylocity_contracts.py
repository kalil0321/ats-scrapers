"""Pipeline contracts for Paylocity."""

from __future__ import annotations

import csv
from pathlib import Path


def test_paylocity_seed_catalogue_is_validated_provider_only() -> None:
    with Path("ats-companies/paylocity.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 9_904
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(
        row["url"]
        == (
            "https://recruiting.paylocity.com/Recruiting/Jobs/All/"
            + row["slug"]
        )
        for row in rows
    )
