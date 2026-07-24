"""Shared contracts for Foundit and TimesJobs."""

from __future__ import annotations

import pytest

from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.foundit import FounditScraper
from ats_scrapers.scrapers.timesjobs import TimesJobsScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS


@pytest.mark.parametrize(
    ("scraper_type", "slug"),
    [(FounditScraper, "in"), (TimesJobsScraper, "all")],
)
def test_scrapers_preserve_standard_options(
    scraper_type: type[BaseScraper], slug: str
) -> None:
    scraper = scraper_type(
        slug,
        include_descriptions=False,
        proxy="http://proxy.example:8080",
    )
    assert scraper.include_descriptions is False
    assert scraper.proxy == "http://proxy.example:8080"


def test_dedup_priorities_prefer_employer_sources() -> None:
    assert ATS_DEDUP_PRIORITY["foundit"] > ATS_DEDUP_PRIORITY["workday"]
    assert ATS_DEDUP_PRIORITY["timesjobs"] > ATS_DEDUP_PRIORITY["workday"]


def test_fail_closed_configs_are_active() -> None:
    assert CONFIGS["foundit"]["fail_closed_on_any_error"] is True
    assert CONFIGS["foundit"]["skip_description_enrichment"] is True
    assert CONFIGS["timesjobs"]["fail_closed_on_empty"] is True
