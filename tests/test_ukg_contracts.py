"""Pipeline contracts for UKG Pro Recruiting."""

from __future__ import annotations

from pathlib import Path


def test_ukg_seed_catalogue_is_provider_only() -> None:
    rows = Path("ats-companies/ukg.csv").read_text().splitlines()

    assert len(rows) >= 462
    assert all("ultipro." in row for row in rows[1:])
