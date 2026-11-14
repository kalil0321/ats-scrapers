from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers import GreetingScraper
from ats_scrapers.source_kind import source_kind
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency

EXPECTED_ROWS = 348


def test_greeting_pipeline_config() -> None:
    config = CONFIGS["greeting"]
    row = {
        "name": "컬리",
        "slug": "kurly",
        "url": "https://kurly.career.greetinghr.com",
    }

    assert config["scraper"] is GreetingScraper
    assert config["slug"](row) == "kurly"
    assert config["kwargs"](row) == {"company_name": "컬리"}
    assert config["csv"] == "ats-companies/greeting.csv"
    assert config["output"] == "greeting/jobs.csv"
    assert config["dedupe_by_ats_id"] is True
    assert _bounded_concurrency(config, 24) == 4


def test_greeting_is_an_employer_ats() -> None:
    assert source_kind("greeting") == "ats"
    assert ATS_DEDUP_PRIORITY["greeting"] == ATS_DEDUP_PRIORITY["workday"]


def test_greeting_catalogue_is_well_formed() -> None:
    with Path("ats-companies/greeting.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    slugs = [row["slug"] for row in rows]
    assert len(set(slugs)) == len(slugs)
    assert slugs == sorted(slugs, key=lambda slug: (slug.casefold(), slug))
    for row in rows:
        assert row["name"].strip()
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", row["slug"])
        assert row["url"] == f"https://{row['slug']}.career.greetinghr.com"
