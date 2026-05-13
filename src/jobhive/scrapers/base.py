"""Base class and registry for ATS scrapers.

Adding a new scraper:

    from jobhive.scrapers.base import BaseScraper, ScraperRegistry
    from jobhive.models import ATSType

    @ScraperRegistry.register(ATSType.GREENHOUSE)
    class GreenhouseScraper(BaseScraper):
        ats = ATSType.GREENHOUSE

        def fetch(self) -> list[Job]:
            ...

The registry is the only stable lookup mechanism — never import scraper
classes by path from outside the package.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType

if TYPE_CHECKING:
    from collections.abc import Callable

    from jobhive.models import Job


class BaseScraper(ABC):
    """Abstract base for every ATS scraper.

    Subclasses must set the `ats` class attribute and implement `fetch()`.
    """

    ats: ClassVar[ATSType]

    def __init__(self, company_slug: str, *, timeout: float = 30.0) -> None:
        self.company_slug = company_slug
        self.timeout = timeout
        self.include_descriptions = True

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Return all currently active jobs for this company."""

    def get_description(self, job: Job) -> str | None:
        """Fetch or return the best-known description for one job.

        The default implementation is correct for providers whose listing
        payload already includes the full description. Providers that need a
        per-job detail request override this method.
        """
        return job.description

    def enrich_descriptions(self, jobs: list[Job]) -> list[Job]:
        """Fill missing descriptions in ``jobs`` when the provider supports it.

        The default path calls :meth:`get_description` one job at a time.
        High-volume providers can override this with a batched/concurrent
        implementation while keeping the public API stable.
        """
        for job in jobs:
            if job.description:
                continue
            description = self.get_description(job)
            if description:
                job.description = description[:25_000]
        return jobs

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.company_slug!r})"


class ScraperRegistry:
    """Maps `ATSType` → scraper class.

    Filled at import time via the `@register` decorator. Use `get_scraper`
    to look up a scraper by ATS.
    """

    _scrapers: ClassVar[dict[ATSType, type[BaseScraper]]] = {}

    @classmethod
    def register(
        cls, ats: ATSType
    ) -> Callable[[type[BaseScraper]], type[BaseScraper]]:
        def decorator(scraper_cls: type[BaseScraper]) -> type[BaseScraper]:
            cls._scrapers[ats] = scraper_cls
            return scraper_cls

        return decorator

    @classmethod
    def get(cls, ats: ATSType | str) -> type[BaseScraper]:
        ats_enum = ATSType(ats) if isinstance(ats, str) else ats
        try:
            return cls._scrapers[ats_enum]
        except KeyError as exc:
            raise ScraperError(
                f"No scraper registered for {ats_enum.value!r}. "
                f"Available: {sorted(s.value for s in cls._scrapers)}"
            ) from exc

    @classmethod
    def all(cls) -> dict[ATSType, type[BaseScraper]]:
        return dict(cls._scrapers)


def get_scraper(ats: ATSType | str, company_slug: str, **kwargs: object) -> BaseScraper:
    """Convenience: lookup + instantiate in one step."""
    return ScraperRegistry.get(ats)(company_slug, **kwargs)  # type: ignore[arg-type]
