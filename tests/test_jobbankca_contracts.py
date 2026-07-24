"""Integration contracts for the Job Bank Canada scraper."""

from __future__ import annotations

from ats_scrapers.scrapers.jobbankca import JobBankCAScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


def test_jobbank_preserves_standard_options() -> None:
    scraper = JobBankCAScraper(
        "all",
        include_descriptions=False,
        proxy="http://proxy.example:8080",
    )

    assert scraper.include_descriptions is False
    assert scraper.proxy == "http://proxy.example:8080"


def test_jobbank_dedup_priority_prefers_employer_sources() -> None:
    assert ATS_DEDUP_PRIORITY["jobbankca"] > ATS_DEDUP_PRIORITY["workday"]


def test_jobbank_singleton_fails_closed_on_empty() -> None:
    assert CONFIGS["jobbankca"]["fail_closed_on_empty"] is True


def test_seek_is_not_scheduled_for_ingestion() -> None:
    assert "seek" not in CONFIGS
