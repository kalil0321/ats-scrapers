from __future__ import annotations

from ats_scrapers.scrapers import HerpScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_herp_pipeline_uses_validated_tenant_catalog() -> None:
    config = CONFIGS["herp"]
    row = {
        "name": "株式会社HERP",
        "slug": "herpinc",
        "url": "https://herp.careers/v1/herpinc",
    }

    assert config["scraper"] is HerpScraper
    assert config["slug"](row) == "herpinc"
    assert config["csv"] == "ats-companies/herp.csv"
    assert config["output"] == "herp/jobs.csv"
    assert "defer_descriptions_to_cache" not in config
    assert "description_cache_path" not in config
    assert config["max_concurrency"] == 4
    assert config["fail_closed_on_empty"] is True


def test_herp_is_a_direct_employer_ats_for_deduplication() -> None:
    assert ATS_DEDUP_PRIORITY["herp"] == ATS_DEDUP_PRIORITY["workday"]
