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
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import socket
from datetime import UTC, datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from ats_scrapers.cloud_browser import CloudBrowser
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

PAGE_SIZE = 12  # Avature's default page size.
MAP_PAGE_SIZE = 30  # SearchJobsMaps pages use a different offset scheme.
MAX_PAGES = 1_000  # Runaway guard; a walk also stops on a page with no new jobs.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
# How many JobDetail pages to fetch concurrently per tenant. Avature
# tenants are mid-sized (most under 200 jobs), so 6 keeps load light
# without prolonging the per-tenant runtime.
DETAIL_CONCURRENCY = 6
# Job pages a cloud browser renders at once.
BROWSER_DETAIL_CONCURRENCY = 4

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


@ScraperRegistry.register(ATSType.AVATURE)
class AvatureScraper(BaseScraper):
    """Avature scraper. ``company_slug`` is either a bare slug
    (``"bloomberg"`` → ``https://bloomberg.avature.net``) or a full base URL
    for tenants on custom domains (``"https://careers.ibm.com/en_US"``).

    ``cloud_browser`` loads the boards that refuse both httpx and httpcloak.
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

        seen: set[str] = set()
        all_jobs: list[Job] = []
        page_size = _page_size(base)
        offset, url = 0, None
        for page_num in range(MAX_PAGES):
            try:
                html_text, page_url = await asyncio.to_thread(
                    self._fetch_page_via_httpcloak_sync, base, offset, url
                )
            except _BlockedTenantError:
                if page_num == 0:
                    raise  # Never made it past page 0 — escalate.
                break  # Mid-pagination block — keep what we have.
            page_jobs = self._parse_page(html_text, base, company)
            if page_num == 0 and not page_jobs:
                _check_empty_listing(html_text, page_url, base)
            new = [j for j in page_jobs if j.ats_id not in seen]
            if not new:
                break
            for j in new:
                seen.add(j.ats_id)
            all_jobs.extend(new)
            following = _next_page(
                html_text, page_url, offset, len(page_jobs), page_size
            )
            if following is None:
                break
            offset, url = following
        else:
            raise _page_guard_error(base, len(all_jobs))

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

        try:
            return httpcloak.get(
                url, headers=_BROWSER_HEADERS, timeout=self.timeout,
                **self._httpcloak_proxy(),
            )
        except Exception:
            return None

    def _httpcloak_proxy(self) -> dict[str, str]:
        return {"proxy": self.proxy} if self.proxy else {}

    async def _fetch_direct(self, base: str, company: str) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, proxy=self.proxy,
        ) as client:
            page_size = _page_size(base)
            offset, url = 0, None
            for page_num in range(MAX_PAGES):
                html_text, page_url = await self._fetch_page(
                    client, base, offset, url
                )
                page_jobs = self._parse_page(html_text, base, company)
                if page_num == 0 and not page_jobs:
                    _check_empty_listing(html_text, page_url, base)
                new = [j for j in page_jobs if j.ats_id not in seen]
                if not new:
                    break
                for j in new:
                    seen.add(j.ats_id)
                all_jobs.extend(new)
                following = _next_page(
                    html_text, page_url, offset, len(page_jobs), page_size
                )
                if following is None:
                    break
                offset, url = following
            else:
                raise _page_guard_error(base, len(all_jobs))

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
        """The listing and detail flow, with each page rendered by ``browser``."""
        all_jobs: list[Job] = []
        seen: set[str] = set()
        page_size = _page_size(base)
        offset, url = 0, None
        for page_num in range(MAX_PAGES):
            list_url = url or _paginated_search_url(base, offset)
            try:
                page = await browser.render(list_url)
            except Exception:
                if page_num == 0:
                    raise
                break
            html_text, page_url = page.html, page.url or list_url
            page_jobs = self._parse_page(html_text, base, company)
            if page_num == 0 and not page_jobs:
                _check_empty_listing(html_text, page_url, base)
            new = [j for j in page_jobs if j.ats_id not in seen]
            if not new:
                break
            for j in new:
                seen.add(j.ats_id)
            all_jobs.extend(new)
            following = _next_page(
                html_text, page_url, offset, len(page_jobs), page_size
            )
            if following is None:
                break
            offset, url = following
        else:
            raise _page_guard_error(base, len(all_jobs))

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
    ) -> tuple[str, str]:
        """``(html, final URL)`` of one listing page."""
        list_url = url or _paginated_search_url(base, offset)
        for attempt in range(1, MAX_RETRIES + 1):
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
                # 406 from Avature is usually transient rate-limit-style
                # blocking: retry with backoff. Only the first listing page
                # escalates to the fallback clients once every retry fails.
                if attempt == MAX_RETRIES and offset == 0:
                    raise _BlockedTenantError(base)
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Avature ({base}) returned 406 at offset={offset} "
                        f"after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
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
    parsed = urlparse(_search_url(base))
    page_params = _pagination_params(base, offset)
    page_keys = set(page_params)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in page_keys
    ]
    query_items.extend((key, str(value)) for key, value in page_params.items())
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query_items),
            parsed.fragment,
        )
    )


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


def _host_does_not_resolve(exc: BaseException) -> bool:
    """True when DNS answered that the host has no address.

    A transient resolver failure (``EAI_AGAIN``) does not count.
    """
    no_address = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return current.errno in no_address
        current = current.__cause__ or current.__context__
    return False


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
