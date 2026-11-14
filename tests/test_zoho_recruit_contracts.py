from __future__ import annotations

import csv
import re
from pathlib import Path

from ats_scrapers.scrapers import ZohoRecruitScraper
from ats_scrapers.source_kind import source_kind
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS, _bounded_concurrency

EXPECTED_ROWS = 1352
URL_RE = re.compile(
    r"https://[a-z0-9][a-z0-9-]*\.zohorecruit\.(?:com|eu|in)/jobs/[^/\s?#]+"
)


def test_zoho_recruit_pipeline_config() -> None:
    config = CONFIGS["zoho_recruit"]
    url = "https://acme.zohorecruit.com/jobs/Careers"
    row = {"name": "Acme", "slug": url, "url": url}

    assert config["scraper"] is ZohoRecruitScraper
    assert config["slug"](row) == url
    assert config["slug"]({"name": "Acme", "url": url}) == url
    assert config["slug"]({"name": "Acme", "slug": "", "url": " "}) is None
    assert config["kwargs"](row) == {"company_name": "Acme"}
    assert config["csv"] == "ats-companies/zoho_recruit.csv"
    assert config["output"] == "zoho_recruit/jobs.csv"
    assert _bounded_concurrency(config, 24) == 4


def test_zoho_recruit_is_an_employer_ats() -> None:
    assert source_kind("zoho_recruit") == "ats"
    assert ATS_DEDUP_PRIORITY["zoho_recruit"] == ATS_DEDUP_PRIORITY["workday"]


def test_zoho_recruit_catalogue_is_well_formed() -> None:
    path = Path("ats-companies/zoho_recruit.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == EXPECTED_ROWS
    urls = [row["url"] for row in rows]
    assert len({url.split("/")[2] for url in urls}) == len(urls)
    assert urls == sorted(urls, key=lambda url: (url.casefold(), url))
    for row in rows:
        assert row["name"].strip()
        assert URL_RE.fullmatch(row["url"])
        assert row["slug"] == row["url"]
        assert ZohoRecruitScraper(row["slug"]).portal_url == row["url"]
