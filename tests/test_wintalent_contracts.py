from __future__ import annotations

import csv
from pathlib import Path

from ats_scrapers.scrapers import WinTalentScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency


def test_wintalent_pipeline_is_fail_closed() -> None:
    config = CONFIGS["wintalent"]
    row = {
        "name": "东风汽车集团有限公司",
        "slug": "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608",
        "url": "https://dfmc.hotjob.cn/SU61d501d92f9d24431f65f608",
    }

    assert config["scraper"] is WinTalentScraper
    assert config["slug"](row) == row["slug"]
    assert config["kwargs"](row) == {"company_name": row["name"]}
    assert config["csv"] == "ats-companies/wintalent.csv"
    assert config["output"] == "wintalent/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert config["max_concurrency"] == 6
    assert config["fail_closed_on_empty"] is True
    assert config["fail_closed_on_any_error"] is True


def test_wintalent_concurrency_cap_is_enforced() -> None:
    assert _bounded_concurrency(CONFIGS["wintalent"], 24) == 6


def test_wintalent_is_a_direct_employer_ats() -> None:
    assert ATS_DEDUP_PRIORITY["wintalent"] == ATS_DEDUP_PRIORITY["workday"]


def test_wintalent_catalog_contains_only_selected_unique_portals() -> None:
    with Path("ats-companies/wintalent.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 36
    assert len({row["slug"] for row in rows}) == len(rows)
    assert len({row["url"] for row in rows}) == len(rows)
    assert all(row["name"].strip() for row in rows)
    assert all(row["slug"] == row["url"] for row in rows)
    assert all(row["url"].startswith("https://") for row in rows)
    assert all(row["url"].split("/", 3)[2].endswith("hotjob.cn") for row in rows)

    urls = {row["url"] for row in rows}
    assert "https://wecruit.hotjob.cn/SU6491506a2f9d24316e91b81b" not in urls
    assert "https://wecruit.hotjob.cn/SU645b1c2ebef57c0907ea0622" not in urls
    assert "https://wecruit.hotjob.cn/SU614bda3abef57c54dcbaf22f" not in urls
    assert "https://www.hotjob.cn/wt/caict" not in urls
