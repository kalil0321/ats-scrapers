"""BrighterMonday: the job board of Kenya and Uganda."""

from __future__ import annotations

from typing import ClassVar

from ats_scrapers.models import ATSType
from ats_scrapers.scrapers._listings_board import ListingsBoardScraper
from ats_scrapers.scrapers.base import ScraperRegistry


@ScraperRegistry.register(ATSType.BRIGHTERMONDAY)
class BrighterMondayScraper(ListingsBoardScraper):
    """BrighterMonday: ``ke``, ``ug``, or every site."""

    ats = ATSType.BRIGHTERMONDAY
    sites: ClassVar[dict[str, tuple[str, str]]] = {
        "ke": ("www.brightermonday.co.ke", "Kenya"),
        "ug": ("www.brightermonday.co.ug", "Uganda"),
    }
