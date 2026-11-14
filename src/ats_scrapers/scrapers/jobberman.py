"""Jobberman: the job board of Nigeria and Ghana."""

from __future__ import annotations

from typing import ClassVar

from ats_scrapers.models import ATSType
from ats_scrapers.scrapers._listings_board import ListingsBoardScraper
from ats_scrapers.scrapers.base import ScraperRegistry


@ScraperRegistry.register(ATSType.JOBBERMAN)
class JobbermanScraper(ListingsBoardScraper):
    """Jobberman: ``ng``, ``gh``, or every site."""

    ats = ATSType.JOBBERMAN
    sites: ClassVar[dict[str, tuple[str, str]]] = {
        "ng": ("www.jobberman.com", "Nigeria"),
        "gh": ("www.jobberman.com.gh", "Ghana"),
    }
