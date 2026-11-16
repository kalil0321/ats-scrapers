from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import WinTalentScraper


def test_wintalent_catalog_contains_only_selected_unique_portals() -> None:
    with Path("ats-companies/wintalent.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 33
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["slug"] == row["url"] for row in rows)
    assert all(row["url"].startswith("https://") for row in rows)
    # A Hotjob subdomain, not any domain that ends with the same letters.
    assert all(
        row["url"].split("/", 3)[2].endswith(".hotjob.cn") for row in rows
    )
    names = [row["name"] for row in rows]
    assert names == sorted(names)
    # The scraper accepts every row as it is written.
    for row in rows:
        assert WinTalentScraper(row["slug"]).portal_url == row["url"]

    urls = {row["url"] for row in rows}
    assert "https://wecruit.hotjob.cn/SU6491506a2f9d24316e91b81b" not in urls
    assert "https://wecruit.hotjob.cn/SU645b1c2ebef57c0907ea0622" not in urls
    assert "https://wecruit.hotjob.cn/SU614bda3abef57c54dcbaf22f" not in urls
    assert "https://www.hotjob.cn/wt/caict" not in urls
    # Closed on 13 Nov 2026: the host is gone from DNS and the shared host
    # answers "该官网已关闭" for the suite.
    assert "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608" not in urls
