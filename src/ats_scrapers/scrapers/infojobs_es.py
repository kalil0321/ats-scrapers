"""InfoJobs Spain (https://www.infojobs.net) — Spain's #1 job board scraper.

InfoJobs Spain (``infojobs.net``) is the largest direct-posting jobs
board in Spain: ~59,000 live postings as of 2026-11 with 20 cards
per listing page (plus 2 promoted ones on page 1), so ~2,950 pages
cover the whole catalogue; the page after the last one is empty.
Companies post directly, not aggregated from LinkedIn / Indeed.

Distinct platform from **InfoJobs Brasil** (``infojobs.com.br``): same
brand, completely different domain, HTML structure, employment-type
labels, and currency. The two scrapers share nothing other than the
brand prefix on the module name.

The Spanish site is a React SPA whose listing pages embed the entire
search payload in ``window.__INITIAL_PROPS__ = JSON.parse("…");`` —
nested twice-encoded JSON. Once decoded, ``offers[]`` carries 22
structured records per page with every field we need:

  - ``code``         → ``ats_id`` (deterministic 30-char hex hash)
  - ``link``         → detail URL (relative, protocol-less)
  - ``title``        → ``title``
  - ``description``  → ``description`` (already plain text)
  - ``city``         → ``location``
  - ``companyName``  → ``company``
  - ``contractType`` → Spanish label, mapped to ``employment_type`` + ``commitment``
  - ``workday``      → working-hours label, kept in ``raw["workday"]``
  - ``teleworking``  → "Presencial" / "Híbrido" / "Remoto" / null → ``is_remote``
  - ``publishedAt``  → ``posted_at`` (ISO-8601 with ``Z``)
  - ``salary``       → ``{range:{min,max}, period, currency, type}`` (often absent)

The listing host sits behind Imperva bot protection (``reese84`` cookie,
Geetest captcha). The pages are read in up to three ways, cheapest first:

1. ``httpcloak`` (browser TLS fingerprint). An address without the
   ``reese84`` cookie gets about five pages, then a "No podemos
   identificar tu navegador" page until the next day.
2. ``cloakbrowser`` (local stealth Chromium). Its first page runs the
   Imperva script and earns the cookie; the remaining pages are fetched
   from inside that page, about one every 1.7 s. Faster than about one a
   second, Imperva answers 405 with a captcha, which clears after a pause.
3. The ``cloud_browser`` given to the scraper, same flow, when
   cloakbrowser is missing or refused.

Each step starts at the page the previous one stopped at. Imperva refuses
a browser session after about 1,000 pages, so cloakbrowser opens a new
session every ``BROWSER_SESSION_PAGES`` pages, moving over ``proxies``.

The listing is sorted by publication date only, so a long read drifts:
postings come and go while it pages. Page 1 of the whole listing lists
every province with its count (``aggregation.province``, the 108 of them
add up to the total); each province is then read on its own
(``?provinceIds=<id>&page=N``, the largest under 650 pages), up to the
``navigation.totalPages`` its first page gives. A posting listed in two
provinces is kept once. A page answered as another one (``navigation.self``)
counts as refused. Without the province counts, the whole listing is read
and ``overview.totalElements`` ends it.

A read that stops before the end raises :class:`ScraperError`, unless
it missed at most ``MAX_MISSING_SHARE`` of the catalogue: then what it
read is returned with a warning.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ats_scrapers.cloud_browser import CDPCloudBrowser
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.fetch import proxy_url_from_env
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import _cloakbrowser as cb
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Any

    from ats_scrapers.models import EngagementType

API_ROOT = "https://www.infojobs.net"
LISTING_URL = f"{API_ROOT}/ofertas-trabajo"
# ~59k offers / 20 per page = ~2,975 pages, plus the first page of each
# of ~108 province slices.
DEFAULT_MAX_PAGES = 4500
# Retry knobs for the httpcloak transport. This scraper deliberately
# does NOT use the shared Fetcher's cloak engine: InfoJobs treats 403
# as *transient* (Distil challenges deep pagination and backs off),
# which the Fetcher never retries in cloak mode.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Pause after each page read in a browser. Measured from one address on
# 25 Nov 2026: one page every 1.5 s read 80 pages without a captcha, one a
# second drew a captcha about every 30 pages.
BROWSER_PAGE_DELAY = 1.2
# Waits before a refused page is read again in the browser. A 30 s pause
# cleared every captcha seen while measuring.
BROWSER_BLOCK_WAITS = (30.0, 60.0, 120.0)
# Imperva refused each browser session about 1,000 pages after it started
# (cloakbrowser and the cloud browser alike, 25 Nov 2026): a session hands
# over to a new one before that.
BROWSER_SESSION_PAGES = 800
# Local browser sessions one read may open.
MAX_BROWSER_SESSIONS = 12
# Share of the catalogue a stopped read may miss and still return what it
# read (with a warning): one small province refused should not drop 59k jobs.
MAX_MISSING_SHARE = 0.02
# How long the first page may take to run the Imperva script and reload.
BROWSER_CHALLENGE_TIMEOUT = 30.0
# Time ``page.evaluate`` gets past the in-page fetch's own deadline before
# the read counts as stalled.
BROWSER_READ_GRACE = 5.0
# The abort covers the body too: a response that stalls mid-body ends.
_FETCH_IN_PAGE_JS = """async ([url, ms]) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
        const r = await fetch(url, {credentials: 'include', signal: controller.signal});
        return {status: r.status, body: await r.text()};
    } catch (e) {
        return {status: 0, body: '', error: String(e)};
    } finally {
        clearTimeout(timer);
    }
}"""

# Spanish-Spain employment-type labels → canonical EmploymentType enum.
# Keys are the literal strings InfoJobs Spain ships on ``contractType``.
# Any new label surfaces as None (logged once via _log_unknown) rather
# than being silently mis-classified.
_CONTRACT_TYPE_MAP: dict[str, str] = {
    "Contrato indefinido": "FULL_TIME",
    "Indefinido": "FULL_TIME",
    "Contrato de duración determinada": "TEMPORARY",
    "Duración determinada": "TEMPORARY",
    "Contrato fijo discontinuo": "TEMPORARY",
    "Fijo discontinuo": "TEMPORARY",
    "Contrato de prácticas": "INTERN",
    "Prácticas": "INTERN",
    "Becario": "INTERN",
    "De formación": "INTERN",
    "Contrato formativo": "INTERN",
    "Contrato autónomo": "CONTRACT",
    "Autónomo": "CONTRACT",
    "Otros contratos": "CONTRACT",
    "Otro tipo de contrato": "CONTRACT",
    "A tiempo parcial": "PART_TIME",
}

_CONTRACT_ENGAGEMENT: dict[str, EngagementType] = {
    "indefinido": "permanent",
    "fijo discontinuo": "permanent",
    "duración determinada": "fixed_term",
    "prácticas": "internship",
    "autónomo": "independent_contractor",
}

# Working-method label → is_remote. "Remoto" / "100% remoto" → fully
# remote; "Híbrido" → False (role isn't fully remote); "Presencial" →
# False; null/missing → None (don't infer). Keep the strings verbatim
# in ``raw["modality"]`` so consumers can recover the original signal.
_REMOTE_TELEWORKING = {"Remoto", "100% remoto", "Teletrabajo", "Remote"}
_ONSITE_TELEWORKING = {"Presencial", "Híbrido", "Hybrid", "Mixto"}

# The hydration payload lives inside ``window.__INITIAL_PROPS__ =
# JSON.parse("…");`` — a JS string literal containing a JSON document.
# We extract the string literal with a balanced walker (regex won't
# do; the payload contains escaped quotes), then ``json.loads`` twice.
_INITIAL_PROPS_MARKER = "window.__INITIAL_PROPS__"
_JSON_PARSE_OPEN = 'JSON.parse('


@ScraperRegistry.register(ATSType.INFOJOBSES)
class InfoJobsSpainScraper(BaseScraper):
    """InfoJobs Spain (infojobs.net) — Spain's #1 direct-posting jobs board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"spain"``) — the scraper paginates the entire
    catalogue.

    Knobs:

    - ``max_pages`` — pagination cap (default 3500 → roughly the full
      ~59k-offer catalogue with headroom).
    - ``listing_url`` — override the base URL when restricting to a
      city / category. ``?page=N`` is appended by the scraper; don't
      include it in the override.
    - ``cloud_browser``: hosted browser for the pages cloakbrowser
      cannot read.
    - ``proxies``: routes the browser sessions move over, one per session
      (``None`` is the direct connection).
    """

    ats = ATSType.INFOJOBSES

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        proxies: Sequence[str | None] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        listing_url: str = LISTING_URL,
        cloud_browser: CDPCloudBrowser | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if cloud_browser is not None and not isinstance(cloud_browser, CDPCloudBrowser):
            raise TypeError(
                "cloud_browser must be a CDPCloudBrowser (InfoJobs Spain drives the page), "
                f"not {type(cloud_browser).__name__}"
            )
        self.max_pages = max(1, max_pages)
        self.listing_url = listing_url
        self.cloud_browser = cloud_browser
        # ``""`` is the direct connection: a ``None`` route given in
        # ``proxies`` must not fall back to the environment's proxy.
        self.routes: list[str | None] = (
            [route or "" for route in proxies] if proxies else [proxy]
        )

    def _route_proxy(self, route: str | None) -> str | None:
        return route if route is not None else proxy_url_from_env()

    async def afetch(self) -> list[Job]:
        from importlib.util import find_spec

        has_httpcloak = find_spec("httpcloak") is not None
        has_browser = cb.is_enabled() or self.cloud_browser is not None
        if not has_httpcloak and not has_browser:
            log.warning(
                "InfoJobs Spain: httpcloak or cloakbrowser is required to get "
                "past the bot protection. Install with "
                "`pip install ats-scrapers[scrapers]`. Skipping (returning [])."
            )
            return []

        state = _Catalogue(
            max_pages=self.max_pages,
            sliced="provinceIds" not in dict(parse_qsl(urlsplit(self.listing_url).query)),
        )
        if has_httpcloak:
            try:
                await self._paginate(state, self._fetch_page)
                return _read_to_the_end(state)
            except ScraperError as exc:
                if not has_browser:
                    return _kept_or_raised(state, _stopped(state, exc))
                log.info(
                    "InfoJobs Spain: plain requests stopped at %s (%s); "
                    "reading on in a browser.",
                    state.where, exc,
                )
        try:
            await self._read_in_browser(state)
        except ScraperError as exc:
            return _kept_or_raised(state, exc)
        return _read_to_the_end(state)

    async def _paginate(
        self,
        state: _Catalogue,
        read: Callable[[str], Awaitable[dict[str, Any]]],
        budget: int | None = None,
    ) -> None:
        """Read pages from where ``state`` stands, ``budget`` of them at
        most. A failed read raises and leaves ``state`` on the page to
        read next."""
        done = 0
        while not state.finished and (budget is None or done < budget):
            payload = await read(state.url(self.listing_url))
            state.add_page(payload, self._parse_offer)
            done += 1

    async def _read_in_browser(self, state: _Catalogue) -> None:
        if cb.is_enabled():
            try:
                await self._read_in_cloakbrowser(state)
                return
            except Exception as exc:
                if self.cloud_browser is None:
                    raise _stopped(state, exc) from exc
                log.warning(
                    "InfoJobs Spain: cloakbrowser stopped at %s (%s: %s); "
                    "reading on in the cloud browser.",
                    state.where, type(exc).__name__, str(exc)[:200],
                )
        if self.cloud_browser is None:
            raise _stopped(state, ScraperError("no browser to read on with"))
        cloud_browser = self.cloud_browser
        while True:
            start = state.reads
            try:
                await cloud_browser.run_page(
                    lambda page: self._paginate_in_page(page, state)
                )
                return
            except Exception as exc:
                # Hosted sessions end after a set time: a session that read
                # pages is replaced and the read goes on.
                if state.reads == start:
                    raise _stopped(state, exc) from exc
                log.warning(
                    "InfoJobs Spain: cloud browser stopped at %s (%s: %s); "
                    "opening another session.",
                    state.where, type(exc).__name__, str(exc)[:200],
                )

    async def _read_in_cloakbrowser(self, state: _Catalogue) -> None:
        """Read in local browser sessions, each on the next route: a new
        one every ``BROWSER_SESSION_PAGES`` pages, or when one is refused.
        Raises once every route opened a session that read nothing."""
        from cloakbrowser import launch_async

        idle = 0
        for session in range(MAX_BROWSER_SESSIONS):
            proxy = self._route_proxy(self.routes[session % len(self.routes)])
            browser = await launch_async(
                headless=True, humanize=True, **({"proxy": proxy} if proxy else {}),
            )
            start = state.reads
            try:
                await self._paginate_in_page(
                    await browser.new_page(), state, BROWSER_SESSION_PAGES,
                )
            except Exception as exc:
                idle = 0 if state.reads > start else idle + 1
                if idle >= len(self.routes):
                    raise
                log.warning(
                    "InfoJobs Spain: browser session %d stopped at %s (%s: %s); "
                    "opening another.",
                    session + 1, state.where, type(exc).__name__, str(exc)[:200],
                )
            finally:
                await browser.close()
            if state.reads > start:
                idle = 0
            if state.finished:
                return
        raise ScraperError(
            f"InfoJobs Spain: {MAX_BROWSER_SESSIONS} browser sessions did not finish the listing"
        )

    async def _paginate_in_page(
        self, page: Any, state: _Catalogue, budget: int | None = None,
    ) -> None:
        """Open the listing in ``page`` so Imperva hands it a cookie, then
        fetch the remaining pages from inside it, ``budget`` at most."""
        await self._clear_challenge(page, state.url(self.listing_url))

        async def read(url: str) -> dict[str, Any]:
            waits = iter(BROWSER_BLOCK_WAITS)
            while True:
                answer = await self._fetch_in_page(page, url)
                status, body = answer.get("status"), answer.get("body") or ""
                wrong = None
                if status == 200 and _INITIAL_PROPS_MARKER in body:
                    props = _extract_initial_props(body)
                    if (wrong := _wrong_page(props, url)) is None:
                        await asyncio.sleep(BROWSER_PAGE_DELAY)
                        return props
                if wrong:
                    cause = wrong
                elif not status:
                    cause = f"InfoJobs Spain: no answer for {url} ({answer.get('error')})"
                elif isinstance(status, int) and status >= 500:
                    cause = f"InfoJobs Spain returned {status} for {url}"
                else:
                    cause = f"InfoJobs Spain: captcha-gated page ({status}) for {url}"
                wait = next(waits, None)
                if wait is None:
                    raise ScraperError(f"{cause} after {len(BROWSER_BLOCK_WAITS)} waits")
                log.info("%s; reading it again in %.0f s.", cause, wait)
                await asyncio.sleep(wait)

        await self._paginate(state, read, budget)

    async def _fetch_in_page(self, page: Any, url: str) -> dict[str, Any]:
        """``{status, body}`` of ``url`` fetched from inside ``page``; status
        0 when no answer came within ``timeout``."""
        try:
            return await asyncio.wait_for(
                page.evaluate(_FETCH_IN_PAGE_JS, [url, int(self.timeout * 1000)]),
                timeout=self.timeout + BROWSER_READ_GRACE,
            )
        except TimeoutError:
            return {"status": 0, "body": "", "error": f"stalled past {self.timeout:g} s"}

    async def _clear_challenge(self, page: Any, url: str) -> None:
        """Load ``url`` until the listing shows. The Imperva script runs on
        the first answer and reloads the page by itself."""
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        deadline = time.monotonic() + BROWSER_CHALLENGE_TIMEOUT
        while True:
            try:
                html = await asyncio.wait_for(page.content(), timeout=self.timeout)
            except Exception:
                # The page is reloading after the challenge.
                html = ""
            if _INITIAL_PROPS_MARKER in html:
                return
            if time.monotonic() >= deadline:
                raise ScraperError(
                    f"InfoJobs Spain: the browser challenge did not clear for {url} "
                    "(captcha-gated)"
                )
            await asyncio.sleep(0.5)

    async def _fetch_page(self, url: str) -> dict[str, Any]:
        props = _extract_initial_props(await self._request_via_httpcloak(url))
        if wrong := _wrong_page(props, url):
            raise ScraperError(wrong)
        return props

    async def _request_via_httpcloak(self, url: str) -> str:
        """Fetch via httpcloak with retry/backoff. Returns the HTML
        body on 200, raises :class:`ScraperError` on hard failures.

        429 and 5xx are retried with exponential backoff; 403 (when
        even httpcloak gets challenged on deep pagination) is treated
        as transient up to ``MAX_RETRIES``."""
        last_status: int | None = None
        proxy = self._route_proxy(self.routes[0])
        for attempt in range(1, MAX_RETRIES + 1):
            result = await asyncio.to_thread(
                _httpcloak_get_sync, url, self.timeout, proxy
            )
            if isinstance(result, str):
                return result
            last_status = result
            if last_status not in (403, 429) and not (500 <= last_status < 600):
                raise ScraperError(
                    f"InfoJobs Spain returned {last_status} for {url}"
                )
            if attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        raise ScraperError(
            f"InfoJobs Spain returned {last_status} for {url} "
            f"after {MAX_RETRIES} retries"
        )

    def _parse_offer(self, offer: dict[str, Any]) -> Job | None:
        code = offer.get("code")
        title = offer.get("title")
        link = offer.get("link")
        if not (isinstance(code, str) and code and isinstance(title, str)
                and title and isinstance(link, str) and link):
            return None

        url = _absolutize_link(link)
        company = (offer.get("companyName") or "").strip() or "Empresa confidencial"
        location = offer.get("city") or None
        if isinstance(location, str):
            location = location.strip() or None

        contract_type_raw = offer.get("contractType")
        contract_label = (
            contract_type_raw.casefold().removeprefix("contrato ").removeprefix("de ")
            if isinstance(contract_type_raw, str) else ""
        )
        employment_type = (
            _CONTRACT_TYPE_MAP.get(contract_type_raw)
            if isinstance(contract_type_raw, str)
            else None
        )

        teleworking_raw = offer.get("teleworking")
        is_remote = _infer_remote(teleworking_raw)

        (salary_min, salary_max, salary_currency, salary_period,
         salary_summary) = _parse_salary(offer.get("salary"))

        posted_at = _parse_published_at(offer.get("publishedAt"))

        description = offer.get("description") or None
        if isinstance(description, str):
            description = description.strip() or None

        workday = offer.get("workday")
        raw: dict[str, Any] = {}
        if isinstance(workday, str) and workday:
            raw["workday"] = workday
        if isinstance(teleworking_raw, str) and teleworking_raw:
            raw["modality"] = teleworking_raw
        if isinstance(contract_type_raw, str) and contract_type_raw:
            raw["contract_type"] = contract_type_raw
        company_link = offer.get("companyLink")
        if isinstance(company_link, str) and company_link:
            raw["company_link"] = company_link
        if offer.get("executive") is True:
            raw["executive"] = True

        return Job(
            url=url,
            title=title.strip(),
            company=company,
            ats_type=ATSType.INFOJOBSES,
            ats_id=code,
            location=location,
            country_iso="ES",
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            engagement_type=_CONTRACT_ENGAGEMENT.get(contract_label),
            commitment=contract_type_raw if isinstance(contract_type_raw, str) else None,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            language="es",
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


@dataclass
class _Slice:
    """A part of the listing read on its own: one province, or the whole
    listing (``province`` None)."""

    province: str | None
    label: str
    count: int | None = None
    # ``navigation.totalPages`` as the slice's first page gives it.
    pages: int | None = None


@dataclass
class _Catalogue:
    """What the reads gathered so far. Each way of reading picks up at
    the slice and page it stands on."""

    max_pages: int
    # Split by province once page 1 of the listing gives the counts.
    sliced: bool = True
    page: int = 1
    jobs: list[Job] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    total: int | None = None
    consecutive_empty: int = 0
    complete: bool = False
    slices: list[_Slice] = field(default_factory=lambda: [_Slice(None, "the listing")])
    slice: int = 0
    reads: int = 0
    planned: bool = False

    @property
    def by_province(self) -> bool:
        return self.slices[0].province is not None

    @property
    def finished(self) -> bool:
        if self.reads >= self.max_pages:
            return True
        if self.by_province:
            return self.slice >= len(self.slices)
        return self.complete or self.consecutive_empty >= 3

    @property
    def where(self) -> str:
        if not self.by_province or self.slice >= len(self.slices):
            return f"page {self.page}"
        return f"{self.slices[self.slice].label} page {self.page}"

    def url(self, listing_url: str) -> str:
        if self.by_province and self.slice < len(self.slices):
            listing_url = _with_province(listing_url, self.slices[self.slice].province)
        return _page_url(listing_url, self.page)

    def add_page(
        self,
        payload: dict[str, Any],
        parse: Callable[[dict[str, Any]], Job | None],
    ) -> None:
        offers = payload.get("offers") or []
        if self.total is None:
            total = (payload.get("overview") or {}).get("totalElements")
            if isinstance(total, int) and total >= 0:
                self.total = total
        new = 0
        for offer in offers:
            job = parse(offer)
            if job is None or job.ats_id in self.seen:
                continue
            self.seen.add(job.ats_id)
            self.jobs.append(job)
            new += 1
        self.reads += 1
        if not self.planned:
            self.planned = True
            if self.sliced and (slices := _province_slices(payload)):
                self.slices, self.slice, self.page = slices, 0, 1
                return
        if self.by_province:
            current = self.slices[self.slice]
            if self.page == 1:
                pages = (payload.get("navigation") or {}).get("totalPages")
                current.pages = pages if isinstance(pages, int) else None
            if not offers or (current.pages is not None and self.page >= current.pages):
                self.slice += 1
                self.page = 1
            else:
                self.page += 1
            return
        self.consecutive_empty = 0 if new else self.consecutive_empty + 1
        # ``totalElements`` is counted when the read starts: postings
        # published during the read leave a gap the empty-page count closes.
        if self.total is not None and len(self.jobs) >= self.total:
            self.complete = True
        self.page += 1

    @property
    def missing_share(self) -> float:
        """Share of the catalogue not read, against the total of page 1."""
        if not self.total:
            return 1.0
        return max(0.0, 1 - len(self.jobs) / self.total)


def _province_slices(payload: dict[str, Any]) -> list[_Slice]:
    """The provinces page 1 counts postings in, largest first; none when
    there is nothing to split."""
    rows = (payload.get("aggregation") or {}).get("province") or []
    slices = [
        _Slice(
            str(row["value"]),
            str(row.get("label") or row["value"]),
            row["count"] if isinstance(row.get("count"), int) else None,
        )
        for row in rows
        if isinstance(row, dict) and str(row.get("value") or "").strip()
    ]
    slices.sort(key=lambda s: -(s.count or 0))
    return slices if len(slices) > 1 else []


def _with_province(listing_url: str, province: str | None) -> str:
    parts = urlsplit(listing_url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "provinceIds"
    ]
    if province:
        query.append(("provinceIds", province))
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment,
    ))


def _wrong_page(props: dict[str, Any], url: str) -> str | None:
    """Why a parsed page is not the one ``url`` asked for, else None: the
    page number (``navigation.self``, within ``totalPages``) or the province
    (``search.provinceIds``) differs."""
    asked = dict(parse_qsl(urlsplit(url).query))
    navigation = props.get("navigation")
    if isinstance(navigation, dict) and str(asked.get("page", "")).isdigit():
        number, got, pages = int(asked["page"]), navigation.get("self"), navigation.get("totalPages")
        if isinstance(got, int) and isinstance(pages, int) and number <= pages and got != number:
            return f"InfoJobs Spain: page {number} answered as page {got} for {url}"
    search = props.get("search")
    answered = search.get("provinceIds") if isinstance(search, dict) else None
    province = asked.get("provinceIds")
    if province and isinstance(answered, list) and province not in [str(p) for p in answered]:
        return f"InfoJobs Spain: {url} answered for provinces {answered}"
    return None


def _stopped(state: _Catalogue, cause: BaseException) -> ScraperError:
    total = f"{state.total:,}" if state.total is not None else "an unknown number of"
    return ScraperError(
        f"InfoJobs Spain: stopped at {state.where} after {len(state.jobs):,} "
        f"of {total} jobs: {cause}"
    )


def _read_to_the_end(state: _Catalogue) -> list[Job]:
    """The jobs of a read that ended; one that ran out of ``max_pages``
    with provinces left is a stopped read."""
    if state.by_province and state.slice < len(state.slices):
        cause = ScraperError(f"the read reached max_pages ({state.max_pages})")
        return _kept_or_raised(state, _stopped(state, cause))
    return state.jobs


def _kept_or_raised(state: _Catalogue, error: ScraperError) -> list[Job]:
    """What a stopped read gathered, when it missed little enough."""
    if state.missing_share > MAX_MISSING_SHARE:
        raise error
    log.warning(
        "InfoJobs Spain: kept %s jobs, %.1f%% of the catalogue unread: %s",
        f"{len(state.jobs):,}", state.missing_share * 100, error,
    )
    return state.jobs


def _httpcloak_get_sync(
    url: str, timeout: float, proxy: str | None = None
) -> str | int:
    """Sync ``httpcloak.get`` — returns the page text on 200, the
    bare status int otherwise so the async caller can retry vs.
    escalate. ``timeout`` is forwarded verbatim."""
    import httpcloak

    kwargs: dict[str, str] = {}
    if proxy:
        kwargs["proxy"] = proxy
    r = httpcloak.get(url, timeout=timeout, **kwargs)
    if r.status_code != 200:
        return int(r.status_code)
    content = r.content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def _extract_initial_props(html_text: str) -> dict[str, Any]:
    """Pull ``window.__INITIAL_PROPS__ = JSON.parse("…");`` out of the
    InfoJobs Spain SPA hydration script.

    The payload is JSON-as-string-as-JSON (double-encoded): the outer
    layer is a JS string literal, the inner layer is the actual JSON
    document. We walk the string literal by tracking escapes so the
    embedded ``\\"`` inside JSON keys doesn't terminate the match
    early.

    Returns the decoded dict. Raises :class:`ScraperError` when the
    marker is missing (Distil bounced us to a captcha page) or the
    payload doesn't parse.
    """
    idx = html_text.find(_INITIAL_PROPS_MARKER)
    if idx == -1:
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ not found, the page is "
            "captcha-gated."
        )
    rest = html_text[idx:]
    start = rest.find(_JSON_PARSE_OPEN)
    if start == -1:
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ marker found but "
            "JSON.parse(...) wrapper is missing — site shape changed."
        )
    # Walk forward to the opening quote of the string literal.
    j = start + len(_JSON_PARSE_OPEN)
    while j < len(rest) and rest[j].isspace():
        j += 1
    if j >= len(rest) or rest[j] not in {'"', "'"}:
        raise ScraperError(
            "InfoJobs Spain: JSON.parse argument is not a string "
            "literal — site shape changed."
        )
    quote = rest[j]
    # Walk to the matching closing quote, honoring backslash escapes.
    i = j + 1
    while i < len(rest):
        c = rest[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            break
        i += 1
    if i >= len(rest):
        raise ScraperError(
            "InfoJobs Spain: unterminated JSON.parse string literal."
        )
    quoted = rest[j:i + 1]
    try:
        inner = _decode_js_string_literal(quoted)
        data = json.loads(inner)
    except json.JSONDecodeError as exc:
        raise ScraperError(
            f"InfoJobs Spain: __INITIAL_PROPS__ failed to parse: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ScraperError(
            "InfoJobs Spain: __INITIAL_PROPS__ did not decode to an object."
        )
    return data



def _decode_js_string_literal(literal: str) -> str:
    quote = literal[0]
    if len(literal) < 2 or literal[-1] != quote or quote not in {"'", '"'}:
        raise json.JSONDecodeError("not a quoted string", literal, 0)
    out: list[str] = []
    i = 1
    end = len(literal) - 1
    escapes = {
        '"': '"',
        "'": "'",
        "\\": "\\",
        "/": "/",
        "b": "\\b",
        "f": "\\f",
        "n": "\\n",
        "r": "\\r",
        "t": "\\t",
    }
    while i < end:
        c = literal[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= end:
            raise json.JSONDecodeError("unterminated escape", literal, i)
        esc = literal[i]
        if esc == "u":
            hex_digits = literal[i + 1:i + 5]
            if len(hex_digits) != 4 or not all(
                ch in "0123456789abcdefABCDEF" for ch in hex_digits
            ):
                raise json.JSONDecodeError("invalid unicode escape", literal, i)
            out.append(chr(int(hex_digits, 16)))
            i += 5
            continue
        out.append(escapes.get(esc, esc))
        i += 1
    return "".join(out)

def _page_url(listing_url: str, page: int) -> str:
    parts = urlsplit(listing_url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "page"
    ]
    query.append(("page", str(page)))
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment,
    ))


def _absolutize_link(link: str) -> str:
    """InfoJobs Spain offer links ship in three shapes:

    - ``//www.infojobs.net/path/of-iXXX?args``  (protocol-relative)
    - ``/path/of-iXXX?args``                     (root-relative)
    - ``https://www.infojobs.net/path/...``      (absolute)

    Return a fully-qualified ``https://`` URL in all three cases.
    """
    link = link.strip()
    if link.startswith("//"):
        return f"https:{link}"
    if link.startswith("/"):
        return f"{API_ROOT}{link}"
    if link.startswith("http://") or link.startswith("https://"):
        return link
    # Defensive — unknown shape, prefix the host so we still produce a
    # valid HttpUrl. Tested shapes above cover everything observed.
    return f"{API_ROOT}/{link.lstrip('/')}"


def _infer_remote(teleworking: object) -> bool | None:
    """``teleworking`` is a structured label, not free text. The site
    only ships a known set of values; absence means "not stated"
    which we surface as ``None`` (don't lie to consumers)."""
    if not isinstance(teleworking, str) or not teleworking:
        return None
    if teleworking in _REMOTE_TELEWORKING:
        return True
    if teleworking in _ONSITE_TELEWORKING:
        return False
    return None


def _parse_published_at(raw: object) -> datetime | None:
    """``publishedAt`` ships as ISO-8601 with a ``Z`` suffix, e.g.
    ``2026-05-12T10:41:35Z``. Python's ``fromisoformat`` only handles
    the ``Z`` natively from 3.11+; we normalize manually for safety."""
    if not isinstance(raw, str) or not raw:
        return None
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    except ValueError:
        return None


_SALARY_PERIOD_MAP = {
    "MONTH": "MONTH",
    "YEAR": "YEAR",
    "HOUR": "HOUR",
    "WEEK": "WEEK",
    "DAY": "DAY",
}


def _parse_salary(
    raw: object,
) -> tuple[float | None, float | None, str | None, str | None, str | None]:
    """InfoJobs Spain salary payload shape (when present):

        {"range": {"min": 1200, "max": 1500},
         "period": "MONTH",
         "currency": "EUR",
         "type": "GROSS"}

    Often absent (the field is omitted when the employer didn't
    specify a range). Returns ``(min, max, currency, period, summary)``
    where ``summary`` is a human-readable single-line form for the
    public schema.
    """
    if not isinstance(raw, dict):
        return None, None, None, None, None
    rng = raw.get("range") if isinstance(raw.get("range"), dict) else None
    if not rng:
        return None, None, None, None, None
    smin = rng.get("min")
    smax = rng.get("max")
    smin = float(smin) if isinstance(smin, (int, float)) and smin > 0 else None
    smax = float(smax) if isinstance(smax, (int, float)) and smax > 0 else None
    if smin is None and smax is None:
        return None, None, None, None, None
    currency = raw.get("currency")
    currency = currency if isinstance(currency, str) and len(currency) == 3 else None
    period_raw = raw.get("period")
    period = (
        _SALARY_PERIOD_MAP.get(period_raw)
        if isinstance(period_raw, str) else None
    )
    # Human summary mirrors what the site renders: "1.200 € - 1.500 €
    # / mes". Keep it minimal and locale-agnostic so downstream
    # consumers don't have to parse a Spanish date string.
    parts: list[str] = []
    if smin is not None and smax is not None:
        parts.append(f"{_fmt_amount(smin)} - {_fmt_amount(smax)}")
    elif smin is not None:
        parts.append(f"desde {_fmt_amount(smin)}")
    elif smax is not None:
        parts.append(f"hasta {_fmt_amount(smax)}")
    if currency == "EUR":
        parts.append("€")
    elif currency:
        parts.append(currency)
    if period == "MONTH":
        parts.append("/ mes")
    elif period == "YEAR":
        parts.append("/ año")
    elif period == "HOUR":
        parts.append("/ hora")
    elif period == "WEEK":
        parts.append("/ semana")
    elif period == "DAY":
        parts.append("/ día")
    summary = " ".join(parts) if parts else None
    return smin, smax, currency, period, summary


_AMOUNT_FMT_RE = re.compile(r"(\d)(?=(\d{3})+(?!\d))")


def _fmt_amount(value: float) -> str:
    """Format an integer-like salary as ``1.500`` (Spanish thousands
    separator). Pass-through fractional values as-is — they're rare on
    InfoJobs Spain and the bare repr is fine."""
    if value == int(value):
        s = str(int(value))
        return _AMOUNT_FMT_RE.sub(r"\1.", s)
    return f"{value:.2f}"
