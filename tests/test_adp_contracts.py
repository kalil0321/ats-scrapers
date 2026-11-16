"""Production integration contracts for ADP Workforce Now."""

from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def test_adp_seed_catalogue_is_provider_only() -> None:
    with Path("ats-companies/adp.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    assert len({row["slug"].casefold() for row in rows}) == len(rows)
    assert len({row["url"].casefold() for row in rows}) == len(rows)
    assert all("/" in row["slug"] for row in rows)
    assert all(
        row["url"].startswith(
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
        )
        for row in rows
    )
    for row in rows:
        cid, career_center_id = row["slug"].split("/", 1)
        query = parse_qs(urlparse(row["url"]).query)

        assert query["cid"] == [cid]
        assert query["ccId"] == [career_center_id]
        assert query["lang"][0]
        assert row["name"]
        assert row["name"].casefold() not in {"facebook", "linkedin"}
        assert not row["name"].casefold().endswith((".jpg", ".jpeg", ".png"))
