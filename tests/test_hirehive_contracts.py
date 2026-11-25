from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse


def test_hirehive_catalog_names_each_career_site_by_its_company_label() -> None:
    with Path("ats-companies/hirehive.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert rows
    assert len({row["slug"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    for row in rows:
        address = urlparse(row["url"])
        assert (address.scheme, address.netloc) == ("https", f"{row['slug']}.hirehive.com")
        assert address.path == address.query == address.fragment == ""
