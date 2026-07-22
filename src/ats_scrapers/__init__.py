"""ats-scrapers — open dataset and toolkit for global job market data.

Two layers of progressive disclosure:

1. Dataset client (zero config):
   >>> from ats_scrapers import search
   >>> df = search(query="ml engineer", location="Paris", ats="greenhouse")

2. Per-ATS scrapers (BYO companies):
   >>> from ats_scrapers.scrapers import GreenhouseScraper
   >>> jobs = GreenhouseScraper("anthropic").fetch()

Publishing/orchestration code lives in the repo-only ``pipeline/``
directory and is not part of the installable package.
"""

from ats_scrapers._version import __version__
from ats_scrapers.client import Client, list_ats, search
from ats_scrapers.exceptions import (
    ATSScrapersError,
    CompanyNotFoundError,
    ManifestError,
    ScraperError,
    StorageError,
)
from ats_scrapers.manifest import Manifest
from ats_scrapers.models import ATSType, Company, EmploymentType, Job, Salary, SalaryPeriod

__all__ = [
    "ATSScrapersError",
    "ATSType",
    "Client",
    "Company",
    "CompanyNotFoundError",
    "EmploymentType",
    "Job",
    "Manifest",
    "ManifestError",
    "Salary",
    "SalaryPeriod",
    "ScraperError",
    "StorageError",
    "__version__",
    "list_ats",
    "search",
]
