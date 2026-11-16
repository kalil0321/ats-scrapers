from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.jibe import _normalize_host


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/jibe.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_jibe_catalogue_is_one_https_origin_per_employer() -> None:
    rows = _rows()

    assert len(rows) >= 150
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    for row in rows:
        assert row["slug"] == _normalize_host(row["url"])
        assert row["url"] == f"https://{row['slug']}"
