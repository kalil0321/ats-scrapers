"""Base class and registry for ATS scrapers.

Adding a new scraper:

    from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry
    from ats_scrapers.models import ATSType

    @ScraperRegistry.register(ATSType.GREENHOUSE)
    class GreenhouseScraper(BaseScraper):
        ats = ATSType.GREENHOUSE

        async def afetch(self) -> list[Job]:
            async with self.make_fetcher() as fetch:
                payload = await fetch.get_json(API_URL.format(slug=self.company_slug))
            return [self._parse(item) for item in payload["jobs"]]

The registry is the only stable lookup mechanism — never import scraper
classes by path from outside the package.

Scrapers are async-first: implement :meth:`afetch`. The sync
:meth:`fetch` wrapper works both from plain scripts and from inside a
running event loop (Jupyter, FastAPI, …), where it runs the coroutine
on a private loop in a worker thread instead of crashing in
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import Engine, Fetcher
from ats_scrapers.models import ATSType

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from ats_scrapers.models import Job

T = TypeVar("T")


class BaseScraper(ABC):
    """Abstract base for every ATS scraper.

    Subclasses must set the ``ats`` class attribute and implement
    :meth:`afetch`. The class attributes ``fetch_engine``,
    ``fetch_escalate``, and ``default_headers`` declare what the ATS
    is known to need (see :class:`ats_scrapers.fetch.Fetcher`);
    :meth:`make_fetcher` turns them into a configured fetcher.
    """

    ats: ClassVar[ATSType]

    #: HTTP engine this ATS needs: "httpx" for nearly everything,
    #: "cloak" (httpcloak TLS impersonation) for load balancers that
    #: block plain clients outright.
    fetch_engine: ClassVar[Engine] = "httpx"
    #: Retry a 403/406 through httpcloak and stick with it. For ATSes
    #: that block httpx only on some tenants.
    fetch_escalate: ClassVar[bool] = False
    #: Headers sent on every request (per-request headers merge over).
    default_headers: ClassVar[dict[str, str] | None] = None

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        self.company_slug = company_slug
        self.timeout = timeout
        self.include_descriptions = include_descriptions
        self.proxy = proxy

    @abstractmethod
    async def afetch(self) -> list[Job]:
        """Return all currently active jobs for this company."""

    def fetch(self) -> list[Job]:
        """Sync convenience wrapper around :meth:`afetch`.

        Safe to call from inside a running event loop.
        """
        return self._run_sync(self.afetch())

    def make_fetcher(self, **overrides: Any) -> Fetcher:
        """Build a :class:`Fetcher` from this scraper's declared config.

        Keyword arguments override the defaults derived from the class
        attributes and constructor parameters.
        """
        config: dict[str, Any] = {
            "label": f"{type(self).__name__}({self.company_slug!r})",
            "timeout": self.timeout,
            "engine": type(self).fetch_engine,
            "escalate": type(self).fetch_escalate,
            "headers": type(self).default_headers,
            "proxy": self.proxy,
        }
        config.update(overrides)
        return Fetcher(**config)

    @staticmethod
    def _run_sync(coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion from synchronous code.

        Uses ``asyncio.run`` when no loop is running. Inside a running
        loop (Jupyter cell, async web handler) it runs the coroutine on
        a fresh loop in a worker thread — blocking the caller, as a
        sync call must, but without touching the outer loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

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

    @classmethod
    def has_scraper(cls, ats: ATSType | str) -> bool:
        """Whether a scraper is registered for ``ats``.

        Lets callers skip dataset sources this package can't scrape yet
        (the hosted dataset can list a source before a scraper ships)
        instead of catching :class:`ScraperError` from ``get``:

        >>> ats = [a for a in list_ats() if ScraperRegistry.has_scraper(a)]
        """
        try:
            ats_enum = ATSType(ats) if isinstance(ats, str) else ats
        except ValueError:
            return False
        return ats_enum in cls._scrapers


def get_scraper(ats: ATSType | str, company_slug: str, **kwargs: object) -> BaseScraper:
    """Convenience: lookup + instantiate in one step."""
    return ScraperRegistry.get(ats)(company_slug, **kwargs)  # type: ignore[arg-type]
