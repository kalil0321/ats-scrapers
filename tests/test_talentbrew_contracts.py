from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers.talentbrew import _normalize_host


def _rows() -> list[dict[str, str]]:
    with Path("ats-companies/talentbrew.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_talentbrew_catalogue_is_one_https_origin_per_employer() -> None:
    rows = _rows()

    assert len(rows) >= 19
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["name"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    for row in rows:
        assert row["slug"] == _normalize_host(row["url"])
        assert row["url"] == f"https://{row['slug']}"
