"""Shared contracts for ByteDance and Torre."""

from __future__ import annotations

from ats_scrapers.scrapers.torre import TorreScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_torre_preserves_standard_options() -> None:
    scraper = TorreScraper(
        "all",
        include_descriptions=False,
        proxy="http://proxy.example:8080",
    )
    assert scraper.include_descriptions is False
    assert scraper.proxy == "http://proxy.example:8080"


def test_dedup_priorities_match_source_authority() -> None:
    assert ATS_DEDUP_PRIORITY["bytedance"] == ATS_DEDUP_PRIORITY["workday"]
    assert ATS_DEDUP_PRIORITY["torre"] > ATS_DEDUP_PRIORITY["workday"]


def test_singleton_configs_fail_closed_on_empty() -> None:
    assert CONFIGS["bytedance"]["fail_closed_on_empty"] is True
    assert CONFIGS["torre"]["fail_closed_on_empty"] is True
    assert CONFIGS["torre"]["defer_descriptions_to_cache"] is True
