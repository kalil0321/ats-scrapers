"""Avature scraper.

Avature powers career sites for many enterprises (Bloomberg, IBM, Astellas,
etc.). There is no public JSON API — every tenant serves a server-rendered
search page at:

    GET https://{slug}.avature.net/careers/SearchJobs/
        ?jobOffset={N}&jobRecordsPerPage=12

Some tenants (notably IBM) host on a custom domain like
``careers.ibm.com/en_US/careers/SearchJobs/`` — for those, pass the full
base URL as ``company_slug`` and the path/locale prefix is preserved.

The HTML markup varies between tenants — Bloomberg uses ``article.job``,
IBM uses ``div.job-item``, Astellas uses table rows. We try a chain of
known selectors with a final fallback to plain ``<a href=".../JobDetail/...">``
anchors.

Avature is selective about clients — many tenants 406-block any HTTP/2
client (httpx, curl) at the load-balancer layer. The block is
post-handshake (header order / h2 settings frame). Two fallbacks, in
order of cost:

1. ``httpcloak`` (free, ~1-10s/page): a TLS+h2-impersonation client
   that gets through 80%+ of 406-blocked tenants in our sample.
2. The ``cloud_browser`` passed to the scraper (see
   :mod:`ats_scrapers.cloud_browser`), for the few tenants ``httpcloak``
   still can't pass. Without one, such a tenant raises.

Some tenants refuse any ``jobOffset`` past 2,000 and state only "999+
results": with 406 (Koch), or with a 500 error page (DHL, NVA, Booz Allen).
A board that reaches that offset is read again in slices, one per value of a
search form filter (``?731[]=6040``), split again while a slice still reaches
the offset.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from ats_scrapers.cloud_browser import CloudBrowser
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import HostPacer
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

PAGE_SIZE = 12  # Avature's default page size.
MAP_PAGE_SIZE = 30  # SearchJobsMaps pages use a different offset scheme.
MAX_PAGES = 1_000  # Runaway guard; a walk also stops on a page with no new jobs.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
# Seconds before each new try of a request refused with 406 once the board
# has answered.
RATE_LIMIT_WAITS = (60.0, 240.0)
# How many JobDetail pages to fetch concurrently per tenant. Avature
# tenants are mid-sized (most under 200 jobs), so 6 keeps load light
# without prolonging the per-tenant runtime.
DETAIL_CONCURRENCY = 6
# Job pages a cloud browser renders at once.
BROWSER_DETAIL_CONCURRENCY = 4
# Listing pages requested at once; slices of a large board are read in parallel.
LISTING_CONCURRENCY = 4
# Tenants that cap the listing answer 406 to an offset at or past this, or
# Avature's own 500 error page on every try.
OFFSET_CAP = 2_000
# The image of that error page ("Oops… Something went wrong"). A proxy, a load
# balancer or a gateway that fails shows a page of its own.
_ERROR_PAGE_IMAGE = "/jscore/images/http/fatal.png"
# A filter with more values is not used to split a search: each value costs
# at least one request.
MAX_SPLIT_VALUES = 500
# Share of a capped walk's jobs that may be in no slice of a filter that
# still counts as splitting the search.
COVERAGE_SLACK = 0.01
# Serves the values of a search filter that the form loads as you type.
_OPTIONS_API = "_wizardPortalDatasetSingleColumnOptionsSearchApi"

logger = logging.getLogger(__name__)

# Avature label vocabulary — multiple tenants reword the same concept.
# These map to our standardized columns. All comparisons are
# case-insensitive after stripping punctuation.
_DEPARTMENT_LABELS = {
    "career area", "function/business area", "function", "business unit",
    "department", "category", "occupational area", "job category",
    "team", "discipline",
}
_EMPLOYMENT_TYPE_LABELS = {
    "employment class", "employment type", "work type", "working time",
    "employment status", "type of employment", "contract type", "schedule",
}
_LOCATION_LABELS = {
    "location", "work location(s)", "work location", "office",
    "primary location", "city", "country",
}
_REMOTE_LABELS = {"remote?", "remote", "work mode", "workplace type"}
_REQ_ID_LABELS = {"ref #", "ref. #", "ref no.", "reference number",
                  "requisition id", "req id", "job id", "job ref"}
_POSTED_LABELS = {"date published", "posted date", "publication date",
                  "post date", "date posted"}

_EMPLOYMENT_TYPE_NORMALIZED = {
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "full-time": "FULL_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "part-time": "PART_TIME",
    "internship": "INTERN",
    "intern": "INTERN",
    "contract": "CONTRACT",
    "fixed term": "CONTRACT",
    "fixed-term": "CONTRACT",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
}

# Locale path prefixes that some tenants insert (`careers.ibm.com/en_US/...`).
_LOCALE_PREFIXES = {
    "en_US", "en_GB", "en_CA", "en_AU", "en_IN", "en_SG",
    "fr_FR", "fr_CA", "es_ES", "es_MX", "de_DE", "it_IT",
    "pt_BR", "pt_PT", "zh_CN", "zh_TW", "ja_JP", "ko_KR", "nl_NL",
}

# A link to another listing page and its offset. Portals name the offset
# ``jobOffset``, ``projectOffset``, ``pipelineOffset`` or ``folderOffset``.
_PAGE_LINK_RE = re.compile(
    r'href="([^"]*?[?&;](?:job|project|pipeline|folder)Offset=(\d+)[^"]*)"'
)

# The result total a listing page states: ``aria-label="432 results"`` on the
# results legend, or ``1-20 of 169 results`` in the pagination legend.
_RESULT_COUNT_RES = (
    re.compile(r'<[^>]*\blist-controls__text__legend\b[^>]*>'),
    re.compile(r'jobPaginationLegend[^>]*>[^<]*?(\d[\d.,]*)\s*[^\d<]*<'),
)
_ARIA_COUNT_RE = re.compile(r'\baria-label="\s*(\d[\d.,]*)')

# Names of the job page in detail links. Portals can rename it: Maximus calls
# it ``Job-Description``.
_DETAIL_PAGES = (
    "JobDetail", "ProjectDetail", "PipelineDetail", "FolderDetail", "Job-Description",
)

# Pseudo-anchor texts that aren't real jobs (action buttons rendered as <a>).
_PSEUDO_TITLES = {
    "apply", "apply now", "apply online", "learn more", "view job",
    "view all", "see job", "more info", "details",
}

class _BlockedTenantError(Exception):
    """The tenant refused the listing with 406: try the next client."""


class _ChallengedTenantError(Exception):
    """The tenant answered with an AWS WAF JavaScript challenge, which only a
    browser passes."""


class _OffsetCapError(Exception):
    """The listing refused an offset at or past ``OFFSET_CAP``."""


class _ListingStoppedError(Exception):
    """A page after the first could not be loaded: keep the rows read so far."""


# ``(url, offset) -> (html, final URL)`` of one listing page.
_PageFetch = Callable[[str, int], Awaitable[tuple[str, str]]]
# ``(endpoint, body) -> JSON`` of the filter values API.
_OptionsFetch = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class _Listing:
    """What one walk of a search read."""

    jobs: list[Job] = field(default_factory=list)
    first_html: str = ""
    first_url: str = ""
    # Ids on the first two pages.
    head: list[str] = field(default_factory=list)
    # Offset and ids of the last page that listed jobs.
    tail: tuple[int, list[str]] | None = None
    # The walk stopped at OFFSET_CAP with more jobs to come.
    capped: bool = False
    # The search lists the parent search's jobs at its start and at its last
    # page: the site ignored the filter value, so the slice splits nothing.
    repeats_parent: bool = False


@dataclass
class _Board:
    """A board being read: how to load a page, and what was read so far."""

    fetch: _PageFetch
    base: str
    company: str
    jobs: dict[str, Job] = field(default_factory=dict)
    # ``(param, values)`` that can split a capped search, in the order tried.
    filters: list[tuple[str, list[str]]] = field(default_factory=list)


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

_OPTIONS_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": _BROWSER_HEADERS["Accept-Language"],
    "X-Requested-With": "XMLHttpRequest",
}


@ScraperRegistry.register(ATSType.AVATURE)
class AvatureScraper(BaseScraper):
    """Avature scraper. ``company_slug`` is either a bare slug
    (``"bloomberg"`` → ``https://bloomberg.avature.net``) or a full base URL
    for tenants on custom domains (``"https://careers.ibm.com/en_US"``).

    ``cloud_browser`` loads the boards that refuse both httpx and httpcloak.
    ``pacer`` spaces the requests sent through httpx and httpcloak, shared with
    any other scraper given the same pacer: Avature refuses an address that
    sends too many, on every tenant.
    """

    ats = ATSType.AVATURE

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        cloud_browser: CloudBrowser | None = None,
        pacer: HostPacer | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if cloud_browser is not None and not isinstance(cloud_browser, CloudBrowser):
            raise TypeError(
                "cloud_browser must be an instance of a CloudBrowser subclass, "
                f"not {type(cloud_browser).__name__}"
            )
        self.cloud_browser = cloud_browser
        self.pacer = pacer
        slug = company_slug.strip()
        if slug.startswith(("http://", "https://")):
            self.company_slug = require_http_url(slug, provider="AvatureScraper")
        else:
            self.company_slug = require_host_label(slug, provider="AvatureScraper")

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, proxy=self.proxy,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_with_detail(client, sem, copy)
            if not copy.description:
                sem = asyncio.Semaphore(1)
                await self._enrich_with_detail_via_httpcloak(sem, copy)
            if not copy.description and self.cloud_browser is not None:
                sem = asyncio.Semaphore(1)
                await self._enrich_with_detail_via_browser(self.cloud_browser, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        base = self._resolve_base_url()
        company = _company_from_base(base) or self.company_slug

        try:
            return await self._fetch_direct(base, company)
        except _BlockedTenantError:
            pass  # 406 — try the cheaper fallback first.
        except _ChallengedTenantError:
            # httpcloak cannot run the challenge's JavaScript either.
            return await self._fetch_refused_board(
                base, company,
                f"Avature ({base}) answered with an AWS WAF JavaScript challenge",
            )

        # Cheap fallback: httpcloak's TLS+h2 fingerprint passes the LB on
        # ~80% of the previously 406-blocked tenants we sampled. No paid
        # service involved.
        try:
            return await self._fetch_via_httpcloak(base, company)
        except _BlockedTenantError:
            pass  # rare: even httpcloak couldn't get through.

        return await self._fetch_refused_board(
            base, company,
            f"Avature ({base}) refused the listing (406) to httpx and httpcloak",
        )

    async def _fetch_refused_board(
        self, base: str, company: str, refused: str,
    ) -> list[Job]:
        """Load a board that plain HTTP cannot read in the cloud browser.

        Without one, or when it fails, the board raises: it was refused, which
        says nothing about whether it has jobs.
        """
        if self.cloud_browser is None:
            raise ScraperError(
                f"{refused}; pass a `cloud_browser` to load it in a browser, "
                "or try another network through `proxy`"
            )
        try:
            return await self._fetch_via_browser(self.cloud_browser, base, company)
        except CompanyNotFoundError:
            raise
        except Exception as exc:
            raise ScraperError(f"{refused}; the cloud browser failed: {exc}") from exc

    async def _fetch_via_httpcloak(self, base: str, company: str) -> list[Job]:
        """TLS+h2 impersonation fallback. Free, ~1-10s/page, beats
        Avature's 406 LB block on ~80% of the tenants where httpx
        fails.

        Raises :class:`_BlockedTenantError` when even httpcloak gets
        406'd, the rare case that needs the cloud browser.
        """
        try:
            import httpcloak  # noqa: F401  — availability check
        except ImportError as exc:
            raise ScraperError(
                "Avature 406 fallback needs httpcloak — "
                "`pip install httpcloak`."
            ) from exc

        sem = asyncio.Semaphore(LISTING_CONCURRENCY)

        async def page(url: str, offset: int) -> tuple[str, str]:
            async with sem:
                try:
                    return await asyncio.to_thread(
                        self._fetch_page_via_httpcloak_sync, base, offset, url
                    )
                except _BlockedTenantError:
                    if offset == 0:
                        raise  # Never made it past page 0: escalate.
                    raise _ListingStoppedError() from None

        async def options(endpoint: str, body: dict[str, Any]) -> Any:
            async with sem:
                response = await asyncio.to_thread(
                    self._post_json_via_httpcloak_sync, endpoint, body
                )
            if response is None or response.status_code != 200:
                return None
            return json.loads(response.text)

        all_jobs = await self._read_board(page, options, base, company)

        # Same TLS fingerprint for detail enrichment so we don't lose
        # the descriptions we just unlocked. Best-effort — a single
        # detail failure must not throw away the listing row.
        if self.include_descriptions:
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
            await asyncio.gather(*(
                self._enrich_with_detail_via_httpcloak(sem, job)
                for job in all_jobs
            ))
        return all_jobs

    def _fetch_page_via_httpcloak_sync(
        self, base: str, offset: int, url: str | None = None,
    ) -> tuple[str, str]:
        """``(html, final URL)`` of one listing page."""
        import httpcloak

        for attempt in range(1, MAX_RETRIES + 1):
            self._pace_sync(url or _search_url(base))
            try:
                response = httpcloak.get(
                    url or _search_url(base),
                    params=None if url else _pagination_params(base, offset),
                    headers=_BROWSER_HEADERS,
                    timeout=self.timeout,
                    **self._httpcloak_proxy(),
                )
            except Exception as exc:
                raise ScraperError(
                    f"Avature ({base}) httpcloak fetch failed at "
                    f"offset={offset}: {exc}"
                ) from exc
            if not 500 <= response.status_code < 600 or attempt == MAX_RETRIES:
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        if offset >= OFFSET_CAP and (
            response.status_code == 406 or _is_error_page(response.status_code, response.text)
        ):
            raise _OffsetCapError()
        if response.status_code == 406:
            raise _BlockedTenantError()
        if response.status_code == 404:
            raise CompanyNotFoundError(f"Avature tenant not found: {base}")
        if response.status_code != 200:
            raise ScraperError(
                f"Avature ({base}) httpcloak returned {response.status_code} "
                f"at offset={offset}"
            )
        return response.text, str(response.url or url or _search_url(base))

    async def _enrich_with_detail_via_httpcloak(
        self, sem: asyncio.Semaphore, job: Job,
    ) -> None:
        async with sem:
            try:
                response = await asyncio.to_thread(
                    self._http_get_via_httpcloak_sync, str(job.url)
                )
            except Exception:
                return
        if response is None or response.status_code != 200:
            return
        fields, description = _parse_detail(response.text)
        _apply_detail_to_job(job, fields, description)

    def _http_get_via_httpcloak_sync(self, url: str):
        import httpcloak

        self._pace_sync(url)
        try:
            return httpcloak.get(
                url, headers=_BROWSER_HEADERS, timeout=self.timeout,
                **self._httpcloak_proxy(),
            )
        except Exception:
            return None

    def _post_json_via_httpcloak_sync(self, url: str, body: dict[str, Any]):
        import httpcloak

        self._pace_sync(url)
        try:
            return httpcloak.post(
                url, json=body, headers=_OPTIONS_HEADERS, timeout=self.timeout,
                **self._httpcloak_proxy(),
            )
        except Exception:
            return None

    async def _pace(self, url: str) -> None:
        if self.pacer is not None:
            await self.pacer.wait(url)

    def _pace_sync(self, url: str) -> None:
        if self.pacer is not None:
            self.pacer.wait_sync(url)

    def _httpcloak_proxy(self) -> dict[str, str]:
        return {"proxy": self.proxy} if self.proxy else {}

    async def _fetch_direct(self, base: str, company: str) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=self.proxy,
        ) as client:
            sem = asyncio.Semaphore(LISTING_CONCURRENCY)
            first = True

            async def page(url: str, offset: int) -> tuple[str, str]:
                nonlocal first
                async with sem:
                    read = await self._fetch_page(client, base, offset, url, first=first)
                first = False
                return read

            async def options(endpoint: str, body: dict[str, Any]) -> Any:
                async with sem:
                    await self._pace(endpoint)
                    response = await client.post(
                        endpoint, json=body, headers=_OPTIONS_HEADERS,
                    )
                if response.status_code != 200:
                    return None
                return response.json()

            all_jobs = await self._read_board(page, options, base, company)

            # Per-job detail fetch — Avature's search-results page has
            # only title/location/department; everything else (description,
            # employment type, requisition id, posted date, full location)
            # lives on /JobDetail/. We fetch concurrently with a small
            # semaphore so a slow tenant doesn't stall the pipeline.
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
            await asyncio.gather(*(
                self._enrich_with_detail(client, sem, job)
                for job in all_jobs
            ))
        return all_jobs

    async def _enrich_with_detail(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, job: Job,
    ) -> None:
        async with sem:
            await self._pace(str(job.url))
            try:
                response = await client.get(str(job.url), headers=_BROWSER_HEADERS)
            except httpx.HTTPError:
                return  # Detail enrichment is best-effort; keep the search row.
            if response.status_code != 200:
                return
        fields, description = _parse_detail(response.text)
        _apply_detail_to_job(job, fields, description)

    async def _fetch_via_browser(
        self, browser: CloudBrowser, base: str, company: str,
    ) -> list[Job]:
        """The listing and detail flow, with each page rendered by ``browser``.

        Only filters whose values the search form lists can split a capped
        board here: the browser does not call the filter values API.
        """
        sem = asyncio.Semaphore(BROWSER_DETAIL_CONCURRENCY)

        async def page(url: str, offset: int) -> tuple[str, str]:
            async with sem:
                try:
                    rendered = await browser.render(url)
                except Exception:
                    if offset == 0:
                        raise
                    # The bundled browsers raise on the 406 refusal.
                    if offset >= OFFSET_CAP:
                        raise _OffsetCapError() from None
                    raise _ListingStoppedError() from None
            return rendered.html, rendered.url or url

        all_jobs = await self._read_board(page, None, base, company)

        if self.include_descriptions:
            sem = asyncio.Semaphore(BROWSER_DETAIL_CONCURRENCY)
            await asyncio.gather(*(
                self._enrich_with_detail_via_browser(browser, sem, job)
                for job in all_jobs
            ))
        return all_jobs

    async def _enrich_with_detail_via_browser(
        self, browser: CloudBrowser, sem: asyncio.Semaphore, job: Job,
    ) -> None:
        async with sem:
            try:
                page = await browser.render(str(job.url))
            except Exception:
                return  # Detail enrichment is best-effort; keep the search row.
        fields, description = _parse_detail(page.html)
        _apply_detail_to_job(job, fields, description)

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        base: str,
        offset: int,
        url: str | None = None,
        *,
        first: bool = True,
    ) -> tuple[str, str]:
        """``(html, final URL)`` of one listing page. ``first`` is the board's
        first request; a slice of a capped board starts at offset 0 as well."""
        list_url = url or _paginated_search_url(base, offset)
        for attempt in range(1, MAX_RETRIES + 1):
            await self._pace(list_url)
            try:
                response = await client.get(list_url, headers=_BROWSER_HEADERS)
            except httpx.HTTPError as exc:
                if (
                    _host_does_not_resolve(exc)
                    and not self.proxy
                    and not _environment_proxy()
                ):
                    # Avature drops the DNS record of a closed tenant.
                    raise CompanyNotFoundError(
                        f"Avature site not found: {base} (host does not resolve)"
                    ) from exc
                if (
                    first
                    and attempt == 1
                    and (self.proxy or _environment_proxy())
                    and await _host_is_gone(list_url)
                ):
                    # A proxy reports a host without an address as its own
                    # failure, such as a 500 to the tunnel request.
                    raise CompanyNotFoundError(
                        f"Avature site not found: {base} (host does not resolve)"
                    ) from exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Avature fetch failed for {base} at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(f"Avature site not found: {base}")
            if response.status_code == 200:
                return response.text, str(response.url)
            if response.headers.get("x-amzn-waf-action", "").lower() == "challenge":
                raise _ChallengedTenantError(base)
            if response.status_code == 406:
                if offset >= OFFSET_CAP:
                    raise _OffsetCapError()
                # Only the board's first request escalates to the fallback
                # clients once every retry fails.
                if attempt == MAX_RETRIES and first:
                    raise _BlockedTenantError(base)
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Avature ({base}) returned 406 at offset={offset} "
                        f"after {MAX_RETRIES} retries"
                    )
                if first:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                else:
                    # Once the board has answered, 406 is Avature's rate limit
                    # for the address, over all tenants. It lets the address
                    # through once its recent requests fall under the limit.
                    await asyncio.sleep(RATE_LIMIT_WAITS[min(attempt, len(RATE_LIMIT_WAITS)) - 1])
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    if offset >= OFFSET_CAP and _is_error_page(
                        response.status_code, response.text,
                    ):
                        raise _OffsetCapError()
                    raise ScraperError(
                        f"Avature ({base}) returned {response.status_code} at "
                        f"offset={offset} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Avature ({base}) returned {response.status_code} at offset={offset}"
            )
        raise ScraperError(f"Avature ({base}) exhausted retries at offset={offset}")

    async def _read_board(
        self,
        fetch: _PageFetch,
        options: _OptionsFetch | None,
        base: str,
        company: str,
    ) -> list[Job]:
        """Every job of the board, split by search filter past ``OFFSET_CAP``."""
        board = _Board(fetch, base, company)
        url = _paginated_search_url(base, 0)
        listing = await self._walk(board, url)
        _keep(board.jobs, listing.jobs)
        if listing.capped:
            logger.info(
                "Avature (%s): the listing refuses offsets past %d after %d jobs; "
                "reading it one search filter value at a time",
                base, OFFSET_CAP, len(listing.jobs),
            )
            board.filters = await _search_filters(
                listing.first_html, listing.first_url, options,
            )
            # A filter the board URL already sets keeps its value.
            preset = frozenset(key for key, _ in parse_qsl(urlparse(_search_url(base)).query))
            await self._split(board, url, listing, preset)
        return list(board.jobs.values())

    async def _split(
        self, board: _Board, url: str, listing: _Listing, applied: frozenset[str],
    ) -> set[str]:
        """Read a capped search one filter value at a time; return the ids read.

        A filter is enough once the jobs the capped walk read turned up in its
        slices. Otherwise the next filter is read as well, since a job without
        a value for the filter is in none of its slices.
        """
        found = {j.ats_id for j in listing.jobs}
        missing = set(found)
        slack = len(found) * COVERAGE_SLACK
        for name, values in board.filters:
            if name in applied:
                continue
            parts = await _run_all([
                self._read_slice(
                    board, _with_query(url, [(name, value)]), listing, applied | {name},
                )
                for value in values
            ])
            for ids in parts:
                found |= ids
                missing -= ids
            if len(missing) <= slack:
                return found
        logger.warning(
            "Avature (%s): a search filtered on %s stops at offset %d and no "
            "filter splits it; %d of its first %d jobs are in no slice, so "
            "jobs after them may be missing",
            board.base, sorted(applied) or "nothing", OFFSET_CAP, len(missing),
            len(listing.jobs),
        )
        return found

    async def _read_slice(
        self, board: _Board, url: str, parent: _Listing, applied: frozenset[str],
    ) -> set[str]:
        listing = await self._walk(board, url, parent=parent)
        _keep(board.jobs, listing.jobs)
        if listing.repeats_parent:
            return set()
        if listing.capped:
            return await self._split(board, url, listing, applied)
        return {j.ats_id for j in listing.jobs}

    async def _walk(
        self, board: _Board, url: str, *, parent: _Listing | None = None,
    ) -> _Listing:
        """Page through the search at ``url`` until its last page or the cap.

        A search that lists the first two pages and the last page of
        ``parent`` as ``parent`` does stops there with ``repeats_parent`` set.
        """
        base = board.base
        listing = _Listing()
        seen: set[str] = set()
        page_size = _page_size(base)
        offset, target = 0, url
        for page_num in range(MAX_PAGES):
            try:
                html_text, page_url = await board.fetch(target, offset)
            except _OffsetCapError:
                listing.capped = True
                return listing
            except _ListingStoppedError:
                return listing
            page_jobs = self._parse_page(html_text, base, board.company)
            if page_num == 0:
                listing.first_html, listing.first_url = html_text, page_url
                if not page_jobs:
                    _check_empty_listing(html_text, page_url, base)
            elif not page_jobs and offset >= OFFSET_CAP:
                # A browser shows the 406 refusal as a page without jobs.
                listing.capped = True
                return listing
            new = [j for j in page_jobs if j.ats_id not in seen]
            if not new:
                return listing
            seen.update(j.ats_id for j in new)
            listing.jobs.extend(new)
            listing.tail = (offset, [j.ats_id for j in page_jobs])
            if page_num < 2:
                listing.head = [j.ats_id for j in listing.jobs]
            following = _next_page(
                html_text, page_url, offset, len(page_jobs), page_size
            )
            if following is None:
                return listing
            if (
                page_num == 1
                and parent is not None
                and listing.head == parent.head
                and await self._lists_tail_of(board, url, parent, offset)
            ):
                listing.repeats_parent = True
                return listing
            offset, link = following
            target = link or _with_query(url, _pagination_items(base, offset))
        raise _page_guard_error(base, len(listing.jobs))

    async def _lists_tail_of(
        self, board: _Board, url: str, parent: _Listing, read_to: int,
    ) -> bool:
        """Whether the search at ``url`` lists ``parent``'s last page read at
        the same offset. Pages up to ``read_to`` already match."""
        if parent.tail is None:
            return False
        offset, ids = parent.tail
        if offset <= read_to:
            return True
        try:
            html_text, _ = await board.fetch(
                _with_query(url, _pagination_items(board.base, offset)), offset,
            )
        except (_ListingStoppedError, _OffsetCapError):
            return False
        page = [j.ats_id for j in self._parse_page(html_text, board.base, board.company)]
        # The page may hold more rows than the parent's page link asked for.
        return bool(ids) and page[:len(ids)] == ids

    def _resolve_base_url(self) -> str:
        slug = self.company_slug
        if slug.startswith(("http://", "https://")):
            return slug.rstrip("/")
        return f"https://{slug}.avature.net"

    def _parse_page(self, html_text: str, base: str, company: str) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ScraperError(
                "Avature scraper requires beautifulsoup4. Install with "
                "`pip install ats-scrapers[scrapers]` or `pip install beautifulsoup4`."
            ) from exc

        soup = BeautifulSoup(html_text, "html.parser")
        # Strategy: find all job-detail anchors, then for each walk up to
        # the nearest wrapping container
        # (article / div / li / tr). The wrapper is where title/location/
        # department live as sibling elements. This handles all tenant
        # markups (Bloomberg `article--result`, IBM `div.job-item`, etc.)
        # without maintaining a per-tenant selector list.
        anchors = soup.find_all(
            "a", href=lambda h: bool(h) and _is_detail_href(str(h))
        )
        seen_ids: set[str] = set()
        jobs: list[Job] = []
        for anchor in anchors:
            # Walk up to the first sensible container.
            container = anchor.find_parent(["article", "li", "tr"]) or anchor.find_parent(
                "div",
                class_=lambda v: bool(v) and any(
                    k in str(v).lower() for k in ("job", "result", "listing", "article")
                ),
            )
            element = container or anchor
            job = _parse_job_element(element, anchor, base, company)
            if job is None or job.ats_id in seen_ids:
                continue
            seen_ids.add(job.ats_id)
            jobs.append(job)
        return jobs


def _parse_job_element(
    element: object, anchor: object, base: str, company: str
) -> Job | None:
    href = (anchor.get("href") or "").strip()  # type: ignore[union-attr]
    if not href or not _is_detail_href(href):
        return None

    # Build absolute URL.
    url = (
        href
        if href.startswith(("http://", "https://"))
        else _join_avature_url(base, href)
    )

    # Job ID = path tail for JobDetail/ProjectDetail pages, ``jobId`` for
    # ``JobDetail?jobId=N`` links, or pipelineId for the
    # SearchJobsMaps/PipelineDetail variant.
    parsed_href = urlparse(href)
    query = parse_qs(parsed_href.query)
    ats_id = (query.get("pipelineId") or [""])[0]
    if not ats_id:
        ats_id = href.rsplit("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if ats_id == "JobDetail":
            ats_id = (query.get("jobId") or [""])[0]
    if not ats_id:
        return None

    # Title preference order:
    # 1. A heading inside the wrapper (`<h2>` / `<h3>`).
    # 2. An element with a *title* class.
    # 3. The anchor text itself — fine when the anchor IS the title link
    #    (Bloomberg, IBM), useless when it's an "Apply" button (skip those).
    title = ""
    title_el = (
        element.find(["h2", "h3"])  # type: ignore[union-attr]
        or element.find(  # type: ignore[union-attr]
            class_=lambda v: bool(v) and "title" in str(v).lower()
        )
    )
    if title_el is not None:
        title = title_el.get_text(strip=True)
    if not title:
        anchor_text = anchor.get_text(strip=True)  # type: ignore[union-attr]
        if anchor_text.lower() in _PSEUDO_TITLES:
            return None
        title = anchor_text
    title = re.sub(r"\s+", " ", title).strip()
    if not title or title.lower() in _PSEUDO_TITLES:
        return None

    # Location: any element with a "location" class.
    location: str | None = None
    loc_el = element.find(  # type: ignore[union-attr]
        class_=lambda v: bool(v) and "location" in str(v).lower()
    )
    if loc_el is not None:
        location = re.sub(r"\s+", " ", loc_el.get_text(strip=True)).strip() or None

    # Department: class contains "department" or "category".
    department: str | None = None
    dept_el = element.find(  # type: ignore[union-attr]
        class_=lambda v: bool(v) and any(
            k in str(v).lower() for k in ("department", "category")
        )
    )
    if dept_el is not None:
        department = (
            re.sub(r"\s+", " ", dept_el.get_text(strip=True)).strip() or None
        )

    return Job(
        url=url,
        title=title,
        company=company,
        ats_type=ATSType.AVATURE,
        ats_id=ats_id,
        location=location,
        department=department,
        posted_at=None,
        fetched_at=datetime.now(UTC),
    )


def _search_url(base: str) -> str:
    """Return the SearchJobs URL for default and custom-path portals."""
    parsed = urlparse(base)
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/searchjobs") or lowered.endswith("/searchjobsmaps"):
        search_path = f"{path}/"
    elif not path or path.lstrip("/") in _LOCALE_PREFIXES:
        search_path = f"{path}/careers/SearchJobs/"
    else:
        search_path = f"{path}/SearchJobs/"
    return urlunparse(
        (parsed.scheme, parsed.netloc, search_path, "", parsed.query, parsed.fragment)
    )


def _paginated_search_url(base: str, offset: int) -> str:
    """Build a listing URL while preserving tenant-specific query params."""
    return _with_query(_search_url(base), _pagination_items(base, offset))


def _pagination_items(base: str, offset: int) -> list[tuple[str, str]]:
    return [(key, str(value)) for key, value in _pagination_params(base, offset).items()]


def _with_query(url: str, items: list[tuple[str, str]]) -> str:
    """``url`` with ``items`` appended to its query, replacing same-name params."""
    parsed = urlparse(url)
    keys = {key for key, _ in items}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in keys
    ]
    return urlunparse(parsed._replace(query=urlencode([*query, *items])))


def _keep(jobs: dict[str, Job], found: list[Job]) -> None:
    for job in found:
        jobs.setdefault(job.ats_id, job)


async def _run_all(aws: list[Awaitable[set[str]]]) -> list[set[str]]:
    """Await ``aws`` together. When one fails, the others are cancelled and
    awaited before the error is raised, so none outlives the client."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _search_filters(
    html_text: str, page_url: str, options: _OptionsFetch | None,
) -> list[tuple[str, list[str]]]:
    """``(param, values)`` of each filter of the search form, fewest values first.

    A select lists its values, or (``731[]``) the form loads them as you type
    from the values API, which ``options`` calls. Filters with one value or
    more than ``MAX_SPLIT_VALUES`` are left out. Two-value filters (remote or
    not, full or part time) come last: one of their slices is usually nearly
    the whole search.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    datasources = _datasources(soup) if options is not None else {}
    found: list[tuple[str, list[str]]] = []
    for form in soup.find_all("form"):
        action = urlparse(str(form.get("action") or "")).path.rstrip("/").lower()
        if form.get("role") != "search" and not action.endswith(("/searchjobs", "/searchjobsmaps")):
            continue
        for select in form.find_all("select"):
            name = str(select.get("name") or "")
            if not name:
                continue
            values = [
                str(option.get("value")).strip()
                for option in select.find_all("option")
                if str(option.get("value") or "").strip()
            ]
            settings = datasources.get(name.removesuffix("[]"))
            if not values and settings is not None and options is not None:
                values = await _datasource_values(options, page_url, name, settings)
            values = list(dict.fromkeys(values))
            if 2 <= len(values) <= MAX_SPLIT_VALUES:
                found.append((name, values))
    return sorted(found, key=lambda item: (len(item[1]) == 2, len(item[1])))


def _datasources(soup: Any) -> dict[str, dict[str, Any]]:
    """Settings of the values API for each form field id that uses it."""
    meta = soup.find("meta", attrs={"name": "avature.wizard.registrars"})
    try:
        wizards = json.loads(str(meta.get("content") or "")) if meta else []
    except ValueError:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for wizard in wizards if isinstance(wizards, list) else []:
        fields = wizard.get("fields") if isinstance(wizard, dict) else None
        for item in fields if isinstance(fields, list) else []:
            if not isinstance(item, dict):
                continue
            for registrar in item.get("registrars") or []:
                if (
                    isinstance(registrar, dict)
                    and registrar.get("name") == "DatasetSingleColumnFieldOptionsDatasource"
                    and isinstance(registrar.get("settings"), dict)
                ):
                    found[str(item.get("fieldId"))] = registrar["settings"]
    return found


async def _datasource_values(
    options: _OptionsFetch, page_url: str, name: str, settings: dict[str, Any],
) -> list[str]:
    """Every value of the filter ``name``, or none past ``MAX_SPLIT_VALUES``."""
    parsed = urlparse(page_url)
    portal = str(settings.get("portalUrlPath") or "")
    if not portal.startswith("/"):
        portal = re.split(r"/searchjobs", parsed.path, maxsplit=1, flags=re.IGNORECASE)[0]
    endpoint = f"{parsed.scheme}://{parsed.netloc}{portal.rstrip('/')}/{_OPTIONS_API}"
    body = {
        "fieldId": name.removesuffix("[]"),
        "datasetFieldSchemaFieldId": settings.get("datasetSchemaFieldId"),
        "schemaFieldIdToUseForIndirectFiltering": settings.get(
            "schemaFieldIdToUseForIndirectFiltering"
        ),
        "filters": settings.get("datasetFilters") or [],
        "optionFormatComponents": settings.get("optionFormatComponents") or [],
        "searchIds": None,
        "currentOptionsIds": [],
        "page": 1,
        "limit": MAX_SPLIT_VALUES,
        "searchText": None,
        "exactMatch": False,
        "datasetSchemaSpecId": settings.get("datasetSchemaSpecId"),
    }
    try:
        payload = await options(endpoint, body)
    except Exception:
        return []
    if not isinstance(payload, dict) or payload.get("more"):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    return [
        str(item["id"])
        for item in results
        if isinstance(item, dict) and item.get("id") is not None
    ]


def _page_size(base: str) -> int:
    return MAP_PAGE_SIZE if _is_map_search(base) else PAGE_SIZE


def _page_guard_error(base: str, found: int) -> ScraperError:
    return ScraperError(
        f"Avature ({base}) still lists new jobs after {MAX_PAGES} pages "
        f"({found} jobs); refusing to return a partial list"
    )


def _next_page(
    html_text: str, page_url: str, offset: int, rows: int, page_size: int,
) -> tuple[int, str | None] | None:
    """``(offset, url)`` of the listing page after ``offset``, or None after
    the last. ``url`` is None when the page should be built from the base.
    Links are relative to ``page_url``, the address that served the page.

    The page's own pagination link leads: some portals serve fewer rows than
    ``jobRecordsPerPage`` asks for (6 instead of 12), page by
    ``projectOffset``, or redirect to a host that drops the query. A page
    without such a link continues only when it is full.
    """
    links = [
        (int(number), urljoin(page_url, html.unescape(href)))
        for href, number in _PAGE_LINK_RE.findall(html_text)
        if int(number) > offset
    ]
    if links:
        return min(links)
    if rows >= page_size:
        return offset + page_size, None
    return None


def _pagination_params(base: str, offset: int) -> dict[str, int]:
    if _is_map_search(base):
        return {"pipelineOffset": offset}
    return {"jobOffset": offset, "jobRecordsPerPage": PAGE_SIZE}


def _is_map_search(base: str) -> bool:
    path = urlparse(_search_url(base)).path.rstrip("/").lower()
    return path.endswith("/searchjobsmaps")


def _is_detail_href(href: str) -> bool:
    # Share links (mailto:, social sites) carry a job URL in their query, so
    # only the path counts.
    parsed = urlparse(href)
    if parsed.scheme not in ("", "http", "https"):
        return False
    path = f"{parsed.path}/"
    return any(f"/{page}/" in path for page in _DETAIL_PAGES)


def _check_empty_listing(html_text: str, page_url: str, base: str) -> None:
    """Raise unless a first listing page without job links is an empty board."""
    if "avature" not in html_text.lower():
        if urlparse(page_url).netloc != urlparse(base).netloc:
            raise CompanyNotFoundError(
                f"Avature ({base}) redirects to {page_url}, which is not an Avature site"
            )
        raise ScraperError(f"Avature ({base}) answered a page that is not an Avature listing")
    legend, pagination = _RESULT_COUNT_RES
    # The legend's attributes come in any order: read the count from its tag.
    tag = legend.search(html_text)
    stated = _ARIA_COUNT_RE.search(tag.group(0)) if tag else None
    stated = stated or pagination.search(html_text)
    if stated and int(re.sub(r"\D", "", stated.group(1))) > 0:
        raise ScraperError(
            f"Avature ({base}) lists {stated.group(1)} results on {page_url} "
            "but no job link could be read"
        )


def _join_avature_url(base: str, href: str) -> str:
    """Resolve Avature detail links without duplicating custom base paths."""
    if href.startswith(("http://", "https://")):
        return href
    base = _link_base(base)
    if not href.startswith("/"):
        return f"{base.rstrip('/')}/{href}"

    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_path = parsed.path.rstrip("/")
    if base_path and (href == base_path or href.startswith(f"{base_path}/")):
        return f"{origin}{href}"
    return f"{base.rstrip('/')}{href}"


def _link_base(base: str) -> str:
    parsed = urlparse(base.rstrip("/"))
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/searchjobs") or lowered.endswith("/searchjobsmaps"):
        path = path.rsplit("/", 1)[0]
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", parsed.fragment))


def _parse_detail(html: str) -> tuple[dict[str, str], str | None]:
    """Pull label/value fields and description from a JobDetail HTML page.

    Avature's detail page wraps content in repeated ``article--details``
    blocks. The first one is "General Information" with labelled
    ``article__content__view__field`` rows (e.g. *Career area* → *Risk*).
    Subsequent blocks contain the free-form job body (one or more
    unlabelled field rows).

    The label vocabulary varies per tenant — Bloomberg uses *Function*,
    Astellas uses *Function/Business Area*, IBM uses *Career area*. The
    raw label/value dict is returned so the caller can map across that
    vocab in one place.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ScraperError(
            "Avature scraper requires beautifulsoup4."
        ) from exc

    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    description_parts: list[str] = []

    # ``article--details`` is a class — sometimes on <article>, sometimes
    # on <div>. ``find_all(class_=...)`` handles both element kinds.
    for blk in soup.find_all(class_="article--details"):
        field_rows = [
            d for d in blk.find_all("div")
            if "article__content__view__field" in (d.get("class") or [])
        ]
        labeled_count = 0
        for fr in field_rows:
            lbl_el = fr.find(
                "div", class_="article__content__view__field__label"
            )
            val_el = fr.find(
                "div", class_="article__content__view__field__value"
            )
            label_text = lbl_el.get_text(strip=True) if lbl_el else ""
            if label_text:
                labeled_count += 1
                if val_el is not None:
                    value_text = re.sub(
                        r"\s+", " ", val_el.get_text(" ", strip=True)
                    )
                    if value_text:
                        fields[label_text] = value_text

        # An "info" block has ≥2 labelled rows; everything else is body.
        if labeled_count >= 2:
            continue

        # Unlabelled field rows = description chunks.
        body_added = False
        for fr in field_rows:
            lbl_el = fr.find(
                "div", class_="article__content__view__field__label"
            )
            if lbl_el and lbl_el.get_text(strip=True):
                continue
            val_el = fr.find(
                "div", class_="article__content__view__field__value"
            ) or fr
            text = val_el.get_text(separator="\n", strip=True)
            if text:
                description_parts.append(text)
                body_added = True
        if not field_rows and not body_added:
            content = blk.find("div", class_="article__content") or blk
            text = content.get_text(separator="\n", strip=True)
            if text and len(text) > 100:
                description_parts.append(text)

    description = "\n\n".join(description_parts).strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    return fields, description or None


def _apply_detail_to_job(
    job: Job, fields: dict[str, str], description: str | None,
) -> None:
    """Mutate ``job`` in place with values pulled from the detail page.

    Pydantic models with ``frozen=False`` (the default) allow attribute
    assignment, but we still need to coerce strings to enums or
    datetimes where the schema expects them.
    """
    # Lower-cased field map for vocabulary lookup.
    flat = {k.strip().lower().rstrip(":"): v for k, v in fields.items() if v}

    def first(label_set: set[str]) -> str | None:
        for label, value in flat.items():
            if label in label_set:
                return value
        return None

    # Department: prefer the search-page hint (already set), only
    # backfill if the detail page provides one and the row is empty.
    if not job.department:
        dept = first(_DEPARTMENT_LABELS)
        if dept:
            job.department = dept

    # Location: detail page often has the canonical version (city +
    # state + country) compared to the listing page snippet.
    detail_loc = first(_LOCATION_LABELS)
    if detail_loc and (not job.location or len(detail_loc) > len(job.location)):
        job.location = detail_loc

    # Employment type — coerce to our enum.
    emp_raw = first(_EMPLOYMENT_TYPE_LABELS)
    if emp_raw:
        norm = emp_raw.strip().lower()
        for key, mapped in _EMPLOYMENT_TYPE_NORMALIZED.items():
            if key in norm:
                job.employment_type = mapped  # type: ignore[assignment]
                break

    # Requisition id (employer's own ref).
    req = first(_REQ_ID_LABELS)
    if req and not job.requisition_id:
        job.requisition_id = req

    # Posted date — Avature serves human-formatted dates; try a couple of
    # common forms and fall through silently if none match.
    posted_raw = first(_POSTED_LABELS)
    if posted_raw and not job.posted_at:
        for fmt in (
            "%A, %B %d, %Y",   # Monday, May 4, 2026
            "%B %d, %Y",       # May 4, 2026
            "%d %B %Y",        # 4 May 2026
            "%Y-%m-%d",
            "%m-%d-%y",        # 05-05-26 (Ally)
            "%d/%m/%Y",
        ):
            try:
                job.posted_at = datetime.strptime(posted_raw.strip(), fmt)
                break
            except ValueError:
                continue

    # Remote flag.
    remote_raw = first(_REMOTE_LABELS)
    if remote_raw is not None and job.is_remote is None:
        norm = remote_raw.strip().lower()
        if norm in ("yes", "remote", "fully remote", "true", "y"):
            job.is_remote = True
        elif norm in ("no", "on-site", "onsite", "in office", "in-office", "false", "n"):
            job.is_remote = False

    # Description.
    if description and not job.description:
        # Cap at ~12kB to avoid Pydantic warnings on extreme outliers.
        job.description = description[:25_000]


def _environment_proxy() -> bool:
    """Whether httpx would go through a proxy set in the environment.

    Behind a proxy, a failed lookup may be the proxy's own name.
    """
    return any(
        os.environ.get(name)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    )


def _is_error_page(status: int, text: str) -> bool:
    """Whether a response is the 500 error page of Avature's application."""
    return status == 500 and _ERROR_PAGE_IMAGE in text


_NO_ADDRESS = frozenset({socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)})


def _host_does_not_resolve(exc: BaseException) -> bool:
    """True when DNS answered that the host has no address.

    A transient resolver failure (``EAI_AGAIN``) does not count.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return current.errno in _NO_ADDRESS
        current = current.__cause__ or current.__context__
    return False


async def _host_is_gone(url: str) -> bool:
    """True when DNS answers that the host of ``url`` has no address while
    its parent domain has one, so the resolver itself works."""
    host = urlparse(url).hostname or ""
    labels = host.split(".")
    if len(labels) < 3:
        return False

    async def resolves(name: str) -> bool | None:
        """Whether ``name`` has an address; None when DNS gave no answer."""
        try:
            await asyncio.to_thread(socket.getaddrinfo, name, 443)
        except socket.gaierror as exc:
            return False if exc.errno in _NO_ADDRESS else None
        except OSError:
            return None
        return True

    if await resolves(host) is not False:
        return False
    return await resolves(".".join(labels[1:])) is True


def _company_from_base(base: str) -> str | None:
    """Best-effort company name from an Avature URL.

    ``bloomberg.avature.net`` → ``"Bloomberg"``
    ``careers.ibm.com``       → ``"Ibm"``
    """
    host = (urlparse(base).netloc or "").lower()
    parts = [p for p in host.split(".") if p]
    if not parts:
        return None
    name = parts[0]
    if name in {"careers", "jobs"} and len(parts) > 1:
        name = parts[1]
    return name.replace("-", " ").title()


def _ensure_locale_in_base(base: str) -> str:
    """If the URL's first path segment is a locale, keep it; else strip path."""
    parsed = urlparse(base)
    path_parts = [p for p in parsed.path.split("/") if p]
    if path_parts and path_parts[0] in _LOCALE_PREFIXES:
        return f"{parsed.scheme}://{parsed.netloc}/{path_parts[0]}"
    return f"{parsed.scheme}://{parsed.netloc}"
