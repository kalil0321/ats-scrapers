"""Pipeline contracts for Jobvite."""

from __future__ import annotations

from pathlib import Path


def test_jobvite_seed_catalogue_is_provider_only() -> None:
    rows = Path("ats-companies/jobvite.csv").read_text().splitlines()

    assert len(rows) > 1
    assert all("jobs.jobvite.com" in row for row in rows[1:])
