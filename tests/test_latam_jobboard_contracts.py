"""Shared contracts for the credential-free Latin American job boards."""

from __future__ import annotations

import pytest

from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.elempleo import ElempleoScraper
from ats_scrapers.scrapers.infojobs_br import InfoJobsBrasilScraper
from pipeline.publisher import ATS_DEDUP_PRIORITY


@pytest.mark.parametrize(
    ("scraper_type", "slug"),
    [
        (ElempleoScraper, "co"),
        (InfoJobsBrasilScraper, "all"),
    ],
)
def test_custom_scrapers_preserve_standard_options(
    scraper_type: type[BaseScraper], slug: str
) -> None:
    scraper = scraper_type(
        slug,
        include_descriptions=False,
        proxy="http://proxy.example:8080",
    )

    assert scraper.include_descriptions is False
    assert scraper.proxy == "http://proxy.example:8080"


def test_jobboard_dedup_priorities_prefer_employer_sources() -> None:
    assert ATS_DEDUP_PRIORITY["bumeran"] > ATS_DEDUP_PRIORITY["workday"]
    assert ATS_DEDUP_PRIORITY["elempleo"] > ATS_DEDUP_PRIORITY["workday"]
    assert ATS_DEDUP_PRIORITY["infojobs_br"] > ATS_DEDUP_PRIORITY["workday"]
