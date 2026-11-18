"""ats-scrapers — open dataset and toolkit for global job market data.

Two layers of progressive disclosure:

1. Dataset client (zero config):
   >>> from ats_scrapers import search
   >>> df = search(query="ml engineer", location="Paris", ats="greenhouse")

2. Per-ATS scrapers (BYO companies):
   >>> from ats_scrapers.scrapers import GreenhouseScraper
   >>> jobs = GreenhouseScraper("anthropic").fetch()
"""

from ats_scrapers._version import __version__
from ats_scrapers.client import Client, find_company, list_ats, search
from ats_scrapers.cloud_browser import CloudBrowser, RenderedPage
from ats_scrapers.exceptions import (
    ATSScrapersError,
    CompanyNotFoundError,
    ManifestError,
    ScraperError,
    StorageError,
)
from ats_scrapers.fetch import Fetcher, FetchResponse, MalformedJSONError
from ats_scrapers.manifest import Manifest
from ats_scrapers.models import (
    ATSType,
    Company,
    CompensationComponent,
    EducationRequirement,
    EmploymentType,
    EngagementType,
    Job,
    Occupation,
    Salary,
    SalaryPeriod,
)
from ats_scrapers.resolve import ResolvedCareersUrl, get_scraper_for_url, resolve_careers_url

__all__ = [
    "ATSScrapersError",
    "ATSType",
    "Client",
    "CloudBrowser",
    "Company",
    "CompanyNotFoundError",
    "CompensationComponent",
    "EducationRequirement",
    "EmploymentType",
    "EngagementType",
    "FetchResponse",
    "Fetcher",
    "Job",
    "MalformedJSONError",
    "Manifest",
    "ManifestError",
    "Occupation",
    "RenderedPage",
    "ResolvedCareersUrl",
    "Salary",
    "SalaryPeriod",
    "ScraperError",
    "StorageError",
    "__version__",
    "find_company",
    "get_scraper_for_url",
    "list_ats",
    "resolve_careers_url",
    "search",
]
