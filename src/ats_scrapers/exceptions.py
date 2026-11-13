"""Exception hierarchy for ats-scrapers."""

from typing import Any


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


class DetailFetchError(ScraperError):
    """Raised when a per-job detail request fails.

    ``status_code`` is the HTTP status of the final response, or ``None``
    when no HTTP response was received (transport error, or the request
    could not be built); when a transport exception caused it, it is
    chained as ``__cause__``. ``url`` is the detail URL that was requested.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        url: str,
    ) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(message)

    def __reduce__(self) -> tuple[Any, ...]:
        # BaseException pickles as cls(*args), which cannot supply the required keywords.
        return (
            _rebuild_detail_fetch_error,
            (str(self.args[0]) if self.args else "", self.status_code, self.url),
        )


def _rebuild_detail_fetch_error(
    message: str, status_code: int | None, url: str
) -> DetailFetchError:
    return DetailFetchError(message, status_code=status_code, url=url)
