"""Common constructor contract for the credential-free job boards."""

from __future__ import annotations

import pytest

from ats_scrapers.scrapers.base import BaseScraper
from ats_scrapers.scrapers.elempleo import ElempleoScraper
from ats_scrapers.scrapers.foundit import FounditScraper
from ats_scrapers.scrapers.infojobs_br import InfoJobsBrasilScraper
from ats_scrapers.scrapers.jobbankca import JobBankCAScraper
from ats_scrapers.scrapers.jobthai import JobThaiScraper
from ats_scrapers.scrapers.seek import SeekScraper
from ats_scrapers.scrapers.timesjobs import TimesJobsScraper
from ats_scrapers.scrapers.torre import TorreScraper
from ats_scrapers.scrapers.vietnamworks import VietnamWorksScraper


@pytest.mark.parametrize(
    ("scraper_type", "slug"),
    [
        (ElempleoScraper, "co"),
        (FounditScraper, "in"),
        (InfoJobsBrasilScraper, "all"),
        (JobBankCAScraper, "all"),
        (JobThaiScraper, "all"),
        (SeekScraper, "au"),
        (TimesJobsScraper, "all"),
        (TorreScraper, "all"),
        (VietnamWorksScraper, "all"),
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
