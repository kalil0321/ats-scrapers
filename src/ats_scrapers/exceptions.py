"""Exception hierarchy for ats-scrapers."""


class ATSScrapersError(Exception):
    """Base class for all ats-scrapers errors."""


class ManifestError(ATSScrapersError):
    """Raised when the dataset manifest cannot be fetched or parsed."""


class StorageError(ATSScrapersError):
    """Raised when reading from or writing to remote storage fails."""


class ScraperError(ATSScrapersError):
    """Raised when an ATS scraper fails to fetch or parse jobs."""


class CompanyNotFoundError(ScraperError):
    """Raised when a company is not present on the requested ATS."""
