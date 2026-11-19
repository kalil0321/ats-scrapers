from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse


def test_hiringthing_catalog_names_each_board_by_its_address() -> None:
    with Path("ats-companies/hiringthing.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) >= 4_500
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["url"] == row["slug"] for row in rows)
    for row in rows:
        address = urlparse(row["slug"])
        assert address.scheme == "https"
        assert address.netloc == address.netloc.lower() != ""
        assert address.path == address.query == address.fragment == ""
