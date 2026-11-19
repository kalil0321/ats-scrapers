"""isolved Hire public career-site scraper.

isolved Hire runs on the ApplicantPro platform under its own host: the portal
page, the listing call and the detail call are ApplicantPro's, at
``{tenant}.isolvedhire.com``.
"""

from __future__ import annotations

from typing import ClassVar

from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.applicantpro import ApplicantProScraper
from ats_scrapers.scrapers.base import ScraperRegistry


@ScraperRegistry.register(ATSType.ISOLVED_HIRE)
class IsolvedHireScraper(ApplicantProScraper):
    """Scrape one public isolved Hire employer portal."""

    ats = ATSType.ISOLVED_HIRE
    host: ClassVar[str] = "isolvedhire.com"
