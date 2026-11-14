from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers import FeishuHireScraper
from ats_scrapers.source_kind import source_kind
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency

EXPECTED_ROWS = 291


def test_feishu_hire_pipeline_config() -> None:
    config = CONFIGS["feishu_hire"]
    row = {
        "name": "蔚来",
        "slug": "nio",
        "sites": "index;campus",
        "url": "https://nio.jobs.feishu.cn",
    }

    assert config["scraper"] is FeishuHireScraper
    assert config["slug"](row) == "nio"
    assert config["kwargs"](row) == {"sites": "index;campus", "company_name": "蔚来"}
    assert config["csv"] == "ats-companies/feishu_hire.csv"
    assert config["output"] == "feishu_hire/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert _bounded_concurrency(config, 24) == 4


def test_feishu_hire_is_an_employer_ats() -> None:
    assert source_kind("feishu_hire") == "ats"
    assert ATS_DEDUP_PRIORITY["feishu_hire"] == ATS_DEDUP_PRIORITY["workday"]


def test_feishu_hire_catalogue_is_well_formed() -> None:
    with Path("ats-companies/feishu_hire.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    slugs = [row["slug"] for row in rows]
    assert len(set(slugs)) == len(slugs)
    assert slugs == sorted(slugs, key=lambda slug: (slug.casefold(), slug))
    for row in rows:
        assert row["name"].strip()
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", row["slug"])
        assert row["url"] == f"https://{row['slug']}.jobs.feishu.cn"
        sites = row["sites"].split(";")
        assert sites and all(re.fullmatch(r"[A-Za-z0-9_-]+", site) for site in sites)
        assert len(set(sites)) == len(sites)
        assert not any("refer" in site.lower() for site in sites)
        scraper = FeishuHireScraper(row["slug"], sites=row["sites"])
        assert scraper.sites == tuple(sites)
