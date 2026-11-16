"""Shared contracts for the SEEK and Job Bank public-market scrapers."""

from __future__ import annotations

import pytest

from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.jobbankca import JobBankCAScraper
from ats_scrapers.scrapers.seek import SeekScraper


@pytest.mark.parametrize(
    ("scraper_type", "slug"),
    [(JobBankCAScraper, "all"), (SeekScraper, "au")],
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
