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
2. Browserbase + Playwright CDP (paid, ~$0.10/min): real Chrome,
   guaranteed to render but expensive and occasionally hangs at the
   session-create step. Reserved for the few tenants ``httpcloak``
   still can't crack.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

PAGE_SIZE = 12  # Avature's default page size.
MAX_PAGES = 200  # Defensive upper bound — caps a runaway loop at ~2400 jobs.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
# How many JobDetail pages to fetch concurrently per tenant. Avature
# tenants are mid-sized (most under 200 jobs), so 6 keeps load light
# without prolonging the per-tenant runtime.
DETAIL_CONCURRENCY = 6

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

# Pseudo-anchor texts that aren't real jobs (action buttons rendered as <a>).
_PSEUDO_TITLES = {
    "apply", "apply now", "apply online", "learn more", "view job",
    "view all", "see job", "more info", "details",
}

class _BlockedTenantError(Exception):
    """Raised when a tenant returns 406 — escalates to the Browserbase path."""


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
    for tenants on custom domains (``"https://careers.ibm.com/en_US"``)."""

    ats = ATSType.AVATURE

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_with_detail(client, sem, copy)
            if not copy.description:
                sem = asyncio.Semaphore(1)
                await self._enrich_with_detail_via_httpcloak(sem, copy)
            return copy.description

        return asyncio.run(run())

    async def _fetch_async(self) -> list[Job]:
        base = self._resolve_base_url()
        company = _company_from_base(base) or self.company_slug

        try:
            return await self._fetch_direct(base, company)
        except _BlockedTenantError:
            pass  # 406 — try the cheaper fallback first.

        # Cheap fallback: httpcloak's TLS+h2 fingerprint passes the LB on
        # ~80% of the previously 406-blocked tenants we sampled. No paid
        # service involved.
        try:
            return await self._fetch_via_httpcloak(base, company)
        except _BlockedTenantError:
            pass  # rare: even httpcloak couldn't get through.

        # Last resort: real browser via Browserbase Sessions.
        return await self._fetch_via_browserbase_optional(base, company)

    async def _fetch_via_browserbase_optional(
        self, base: str, company: str,
    ) -> list[Job]:
        """Use Browserbase only when configured; otherwise return empty.

        This keeps the scraper usable as a public library without
        forcing a paid dependency. Three states:

        * ``BROWSERBASE_API_KEY`` + ``BROWSERBASE_PROJECT_ID`` set, and
          ``playwright`` importable → run the Browserbase fallback.
        * Either credential missing → log a warning and return ``[]`` for
          this tenant (the rest of the pipeline keeps running).
        * Playwright unavailable → same as above.

        Set ``JOBHIVE_DISABLE_BROWSERBASE=1`` to force the fallback off
        even when credentials exist (useful in CI / local dev).
        """
        import logging
        log = logging.getLogger(__name__)

        if os.getenv("JOBHIVE_DISABLE_BROWSERBASE"):
            log.warning(
                "Avature %s: 406-blocked and "
                "JOBHIVE_DISABLE_BROWSERBASE is set; skipping tenant.",
                base,
            )
            return []

        api_key = os.getenv("BROWSERBASE_API_KEY")
        project_id = os.getenv("BROWSERBASE_PROJECT_ID")
        if not api_key or not project_id:
            log.warning(
                "Avature %s: 406-blocked. Set BROWSERBASE_API_KEY and "
                "BROWSERBASE_PROJECT_ID to enable the fallback path; "
                "skipping tenant for now.",
                base,
            )
            return []

        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            log.warning(
                "Avature %s: 406-blocked and `playwright` is not "
                "installed. Run `pip install playwright` to enable the "
                "Browserbase fallback; skipping tenant for now.",
                base,
            )
            return []

        try:
            return await self._fetch_via_browserbase(base, company)
        except Exception as exc:  # log + continue is intentional
            log.warning("Avature %s: Browserbase fallback failed (%s).", base, exc)
            return []

    async def _fetch_via_httpcloak(self, base: str, company: str) -> list[Job]:
        """TLS+h2 impersonation fallback. Free, ~1-10s/page, beats
        Avature's 406 LB block on ~80% of the tenants where httpx
        fails. Verified live on Unifi / sandboxbnc / uop (the same
        tenants that hung Browserbase Sessions in 2026-05).

        Raises :class:`_BlockedTenantError` when even httpcloak gets
        406'd — the rare case that escalates to Browserbase Sessions.
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
        for page_num in range(MAX_PAGES):
            offset = page_num * PAGE_SIZE
            try:
                html_text = await asyncio.to_thread(
                    self._fetch_page_via_httpcloak_sync, base, offset
                )
            except _BlockedTenantError:
                if page_num == 0:
                    raise  # Never made it past page 0 — escalate.
                break  # Mid-pagination block — keep what we have.
            page_jobs = self._parse_page(html_text, base, company)
            new = [j for j in page_jobs if j.ats_id not in seen]
            if not new:
                break
            for j in new:
                seen.add(j.ats_id)
            all_jobs.extend(new)
            if len(page_jobs) < PAGE_SIZE:
                break

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

    def _fetch_page_via_httpcloak_sync(self, base: str, offset: int) -> str:
        import httpcloak

        url = f"{base}/careers/SearchJobs/"
        try:
            response = httpcloak.get(
                url,
                params={"jobOffset": offset, "jobRecordsPerPage": PAGE_SIZE},
                headers=_BROWSER_HEADERS,
                timeout=self.timeout,
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
        return response.text

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
            )
        except Exception:
            return None

    async def _fetch_direct(self, base: str, company: str) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page_num in range(MAX_PAGES):
                offset = page_num * PAGE_SIZE
                html_text = await self._fetch_page(client, base, offset)
                page_jobs = self._parse_page(html_text, base, company)
                new = [j for j in page_jobs if j.ats_id not in seen]
                if not new:
                    break
                for j in new:
                    seen.add(j.ats_id)
                all_jobs.extend(new)
                # Termination: short page = last page.
                if len(page_jobs) < PAGE_SIZE:
                    break

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

    async def _fetch_via_browserbase(self, base: str, company: str) -> list[Job]:
        """Browserbase fallback: run the same listing+detail flow via real Chrome.

        Cost is non-trivial (a session is ~$0.10/min), so we keep the
        per-tenant work tight: one session, all listing pages first,
        then a small concurrent batch of detail-page navigations on
        separate tabs. We cap detail concurrency at 4 to avoid pushing
        Browserbase past its per-session limits.

        Caller (``_fetch_via_browserbase_optional``) verifies that
        ``BROWSERBASE_API_KEY`` / ``BROWSERBASE_PROJECT_ID`` are set
        and that ``playwright`` is importable — this method assumes both.
        """
        from playwright.async_api import async_playwright

        api_key = os.environ["BROWSERBASE_API_KEY"]
        project_id = os.environ["BROWSERBASE_PROJECT_ID"]

        # Create a Browserbase session.
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.browserbase.com/v1/sessions",
                headers={"X-BB-API-Key": api_key,
                         "Content-Type": "application/json"},
                json={
                    "projectId": project_id,
                    "browserSettings": {
                        "fingerprint": {
                            "browsers": ["chrome"],
                            "devices": ["desktop"],
                            "operatingSystems": ["macos"],
                        }
                    },
                },
            )
            if r.status_code != 201:
                raise ScraperError(
                    f"Browserbase session create failed: {r.status_code} "
                    f"{r.text[:200]}"
                )
            session = r.json()
            ws_url = session["connectUrl"]

        all_jobs: list[Job] = []
        seen: set[str] = set()
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            try:
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                # Listing pages — sequential on one tab.
                for page_num in range(MAX_PAGES):
                    offset = page_num * PAGE_SIZE
                    list_url = (
                        f"{base}/careers/SearchJobs/"
                        f"?jobOffset={offset}&jobRecordsPerPage={PAGE_SIZE}"
                    )
                    try:
                        await page.goto(
                            list_url, wait_until="domcontentloaded", timeout=30_000
                        )
                    except Exception:
                        break
                    html_text = await page.content()
                    page_jobs = self._parse_page(html_text, base, company)
                    new = [j for j in page_jobs if j.ats_id not in seen]
                    if not new:
                        break
                    for j in new:
                        seen.add(j.ats_id)
                    all_jobs.extend(new)
                    if len(page_jobs) < PAGE_SIZE:
                        break

                if self.include_descriptions:
                    # Detail pages — small parallel batch on extra tabs.
                    bb_detail_concurrency = 4
                    sem = asyncio.Semaphore(bb_detail_concurrency)
                    tabs = [await ctx.new_page() for _ in range(bb_detail_concurrency)]
                    tab_q: asyncio.Queue = asyncio.Queue()
                    for t in tabs:
                        tab_q.put_nowait(t)

                    async def enrich(job: Job) -> None:
                        async with sem:
                            tab = await tab_q.get()
                            try:
                                try:
                                    await tab.goto(
                                        str(job.url),
                                        wait_until="domcontentloaded",
                                        timeout=30_000,
                                    )
                                except Exception:
                                    return
                                html = await tab.content()
                                fields, description = _parse_detail(html)
                                _apply_detail_to_job(job, fields, description)
                            finally:
                                tab_q.put_nowait(tab)

                    await asyncio.gather(*(enrich(j) for j in all_jobs))
            finally:
                await browser.close()
        return all_jobs

    async def _fetch_page(
        self, client: httpx.AsyncClient, base: str, offset: int
    ) -> str:
        url = f"{base}/careers/SearchJobs/"
        params = {"jobOffset": offset, "jobRecordsPerPage": PAGE_SIZE}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url, params=params, headers=_BROWSER_HEADERS
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Avature fetch failed for {base} at offset={offset}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(f"Avature site not found: {base}")
            if response.status_code == 200:
                return response.text
            if response.status_code == 406:
                # 406 from Avature is usually transient rate-limit-style
                # blocking — retry with backoff. Only escalate to the
                # Browserbase path on the first listing page if every
                # retry exhausts.
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
                "`pip install jobhive[scrapers]` or `pip install beautifulsoup4`."
            ) from exc

        soup = BeautifulSoup(html_text, "html.parser")
        # Strategy: find all `/JobDetail/` anchors (the canonical job URL
        # form), then for each walk up to the nearest wrapping container
        # (article / div / li / tr). The wrapper is where title/location/
        # department live as sibling elements. This handles all tenant
        # markups (Bloomberg `article--result`, IBM `div.job-item`, etc.)
        # without maintaining a per-tenant selector list.
        anchors = soup.find_all(
            "a", href=lambda h: bool(h) and "/JobDetail/" in h
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
    if not href or "/JobDetail/" not in href:
        return None

    # Build absolute URL.
    if href.startswith(("http://", "https://")):
        url = href
    else:
        url = f"{base}{href if href.startswith('/') else '/' + href}"

    # Job ID = last non-empty path segment (strip query string).
    ats_id = href.rsplit("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
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
        fetched_at=datetime.now(),
    )


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
