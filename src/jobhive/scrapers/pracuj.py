"""Pracuj.pl (https://www.pracuj.pl) — Polish #1 job board scraper.

Pracuj.pl is Poland's largest direct-posting job board (~50k live
postings as of mid-2026). Companies post directly — not syndicated
from LinkedIn / Indeed. Listings are served from a Next.js app whose
hydration payload (`<script id="__NEXT_DATA__">…</script>`) embeds
the entire structured job feed for the page: title, company, salary
text, contract types, work modes, position levels, and a per-location
breakdown under ``offers[]``.

Pagination is via the ``?pn=N`` query parameter (1-indexed). The
``offersTotalCount`` field on each page tells us how many results
the catalogue has in total, so we can stop early instead of probing
until empty.

Cloudflare protects the site against bare httpx user agents; the
scraper transparently falls back to ``httpcloak`` (TLS+h2 finger-print
impersonation, already shipped in the ``scrapers`` extra) the first
time a direct GET returns 403. Same pattern as Built In / Avature /
JazzHR.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.pracuj.pl"
LISTING_URL = f"{API_ROOT}/praca"
DEFAULT_MAX_PAGES = 1000  # ~50k live offers / 50 per page = ~1000 pages
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Polish-language contract type labels → normalized EmploymentType.
# Pracuj.pl ships these as Polish strings — keep the originals as keys so
# any new contract types added by Pracuj surface as unmapped (logged) rather
# than silently re-classified.
_CONTRACT_TYPE_MAP: dict[str, str] = {
    "Umowa o pracę": "FULL_TIME",
    "Umowa na zastępstwo": "TEMPORARY",
    "Kontrakt B2B": "CONTRACT",
    "Umowa zlecenie": "CONTRACT",
    "Umowa o dzieło": "CONTRACT",
    "Umowa o pracę tymczasową": "TEMPORARY",
    "Staż / Praktyka": "INTERN",
    "Staż": "INTERN",
    "Praktyka": "INTERN",
}

# Work-mode labels → is_remote boolean. "Praca zdalna" is fully remote,
# "Praca hybrydowa" is hybrid (treat as is_remote=True since the role can
# be performed remotely some of the time — same convention as Manfred's
# remotePercentage≥50). "Praca stacjonarna" / "Praca mobilna" are on-site.
_REMOTE_WORK_MODES = {"Praca zdalna", "Praca hybrydowa"}

# __NEXT_DATA__ JSON blob — Next.js' standard hydration script tag.
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


@ScraperRegistry.register(ATSType.PRACUJ)
class PracujScraper(BaseScraper):
    """Pracuj.pl — Poland's largest direct-posting job board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``max_pages`` — pagination cap (default 1000, well above the
      ~700-1000 pages currently in the active catalogue).
    """

    ats = ATSType.PRACUJ

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        # Flipped to True the first time the direct ``httpx`` path
        # returns 403; subsequent requests in this scraper instance
        # then go through ``httpcloak``.
        self._use_httpcloak = False

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for j in items:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 3:
                try:
                    page_jobs = await self._fetch_listing_page(client, sem, page)
                except ScraperError as exc:
                    # Once we have some pages, treat a hard error on a
                    # later page as a soft stop — keep what we have.
                    if page == 1:
                        raise
                    log.warning(
                        "Pracuj.pl: stopping pagination at page %d (%s); "
                        "keeping %d jobs collected so far.",
                        page, exc, len(jobs),
                    )
                    break
                new = sum(1 for j in page_jobs if j.ats_id not in seen)
                await absorb(page_jobs)
                if not page_jobs:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0 if new else consecutive_empty + 1
                page += 1
        return jobs

    async def _fetch_listing_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> list[Job]:
        url = f"{LISTING_URL}?pn={page}"
        text = await self._request_html(client, sem, url)
        return self._parse_listing(text)

    def _parse_listing(self, text: str) -> list[Job]:
        match = _NEXT_DATA_RE.search(text)
        if not match:
            return []
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning("Pracuj.pl: __NEXT_DATA__ payload was not valid JSON")
            return []

        groups = (
            payload.get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("jobOffers", {})
            .get("groupedOffers")
        )
        if not isinstance(groups, list):
            return []

        jobs: list[Job] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            jobs.extend(self._parse_group(group))
        return jobs

    def _parse_group(self, group: dict[str, Any]) -> list[Job]:
        """Each ``groupedOffer`` covers one role; ``offers[]`` enumerates
        the per-location postings (one Job per location). Most groups
        ship a single offer; multi-city ones explode into N jobs."""
        title = (group.get("jobTitle") or "").strip()
        company = (group.get("companyName") or "").strip() or "Unknown"
        offers = group.get("offers") or []
        if not title or not isinstance(offers, list) or not offers:
            return []

        salary_summary = (group.get("salaryDisplayText") or "").strip() or None
        salary_currency = _detect_salary_currency(salary_summary)

        description = _clean_description(group.get("jobDescription"))
        posted_at = _parse_iso(group.get("lastPublicated"))

        contract_types = _filter_str_list(group.get("typesOfContract"))
        employment_type = _map_employment_type(contract_types)
        commitment = ", ".join(contract_types) or None

        work_modes = _filter_str_list(group.get("workModes"))
        is_remote = _infer_remote(work_modes)

        position_levels = _filter_str_list(group.get("positionLevels"))
        work_schedules = _filter_str_list(group.get("workSchedules"))

        raw_common: dict[str, Any] = {}
        if position_levels:
            raw_common["position_levels"] = position_levels
        if work_modes:
            raw_common["work_modes"] = work_modes
        if work_schedules:
            raw_common["work_schedules"] = work_schedules
        if contract_types:
            raw_common["types_of_contract"] = contract_types
        company_id = group.get("companyId")
        if isinstance(company_id, int):
            raw_common["company_id"] = company_id

        jobs: list[Job] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            partition_id = offer.get("partitionId")
            offer_url = (offer.get("offerAbsoluteUri") or "").strip()
            if not partition_id or not offer_url:
                continue
            ats_id = str(partition_id)

            location = _format_location(
                offer.get("displayWorkplace"),
                is_whole_poland=bool(offer.get("isWholePoland")),
            )

            raw = dict(raw_common)
            if offer.get("isWholePoland"):
                raw["is_whole_poland"] = True

            jobs.append(
                Job(
                    url=offer_url,
                    title=title,
                    company=company,
                    ats_type=ATSType.PRACUJ,
                    ats_id=ats_id,
                    location=location,
                    country_iso="PL",
                    language="pl",
                    is_remote=is_remote,
                    salary_currency=salary_currency,
                    salary_period="MONTH" if salary_currency else None,
                    salary_summary=salary_summary,
                    employment_type=employment_type,
                    commitment=commitment,
                    description=description,
                    posted_at=posted_at,
                    fetched_at=datetime.now(),
                    raw=raw or None,
                )
            )
        return jobs

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        # Once a 403 has flipped the instance to httpcloak mode, every
        # subsequent request skips the wasted direct attempt.
        if self._use_httpcloak:
            return await self._request_via_httpcloak(url)

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/124.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,*/*",
                            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Pracuj.pl fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 403:
                # Cloudflare-style block on the bare httpx fingerprint.
                # Flip the scraper into httpcloak mode and retry; every
                # subsequent page in this fetch reuses the cheap path.
                log.info(
                    "Pracuj.pl: 403 on %s — switching to httpcloak fallback",
                    url,
                )
                self._use_httpcloak = True
                return await self._request_via_httpcloak(url)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Pracuj.pl returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Pracuj.pl returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"Pracuj.pl exhausted retries for {url}: {last_exc}"
        )

    async def _request_via_httpcloak(self, url: str) -> str:
        """TLS+h2 impersonation fallback used when pracuj.pl 403's
        the direct httpx user-agent (Cloudflare-protected)."""
        from importlib.util import find_spec

        if find_spec("httpcloak") is None:
            raise ScraperError(
                "Pracuj.pl's 403 fallback needs httpcloak — "
                "`pip install jobhive[scrapers]`."
            )

        last_status: int | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            text = await asyncio.to_thread(self._httpcloak_get_sync, url)
            if isinstance(text, str):
                return text
            last_status = text
            if last_status != 403 or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
        raise ScraperError(
            f"Pracuj.pl httpcloak fallback returned {last_status} for "
            f"{url} after {MAX_RETRIES} retries"
        )

    @staticmethod
    def _httpcloak_get_sync(url: str) -> str | int:
        import httpcloak

        r = httpcloak.get(url, timeout=30)
        if r.status_code != 200:
            return int(r.status_code)
        content = r.content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return content


def _filter_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _format_location(
    workplace: object, *, is_whole_poland: bool
) -> str | None:
    if is_whole_poland:
        # The ATS-side flag means the role accepts candidates from anywhere
        # in Poland. ``workplace`` is usually still set in that case (e.g.
        # the HQ city); we surface both for clarity downstream.
        wp = workplace.strip() if isinstance(workplace, str) and workplace.strip() else None
        return f"{wp}, Poland (cała Polska)" if wp else "Poland (cała Polska)"
    if isinstance(workplace, str) and workplace.strip():
        return workplace.strip()
    return None


def _detect_salary_currency(summary: str | None) -> str | None:
    """Pracuj.pl salary strings are free-text in Polish. Currency lives
    in the trailing label (e.g. ``8 000–21 000 zł brutto / mies.``,
    ``$3000 - $5000 net / month``). We detect a small handful and leave
    the numeric parse to ``jobhive.enrichment.parse_salary_range``."""
    if not summary:
        return None
    s = summary.lower()
    # PLN: 'zł' (with or without the diacritic), 'pln'
    if "zł" in s or "zl" in s.split() or "pln" in s:
        return "PLN"
    if "€" in s or " eur" in s or s.startswith("eur"):
        return "EUR"
    if "$" in s or " usd" in s:
        return "USD"
    if "£" in s or " gbp" in s:
        return "GBP"
    return None


def _map_employment_type(contract_types: list[str]) -> str | None:
    """First mapped contract type wins. Pracuj.pl groups multiple types
    in one card (e.g. 'Umowa o pracę / B2B / Zlecenie'); the canonical
    enum is a single value, so we prefer the most permanent type if it
    is present (FULL_TIME > CONTRACT > TEMPORARY > INTERN)."""
    if not contract_types:
        return None
    mapped = [_CONTRACT_TYPE_MAP.get(c) for c in contract_types]
    mapped = [m for m in mapped if m]
    if not mapped:
        return None
    # Pick by priority — full-time wins over contract wins over temp etc.
    priority = ("FULL_TIME", "PART_TIME", "CONTRACT", "TEMPORARY", "INTERN")
    for p in priority:
        if p in mapped:
            return p
    return mapped[0]


def _infer_remote(work_modes: list[str]) -> bool | None:
    """``workModes`` is the ATS-side flag — we trust it. Empty list
    means the ATS didn't classify the role, so return ``None`` and let
    LLM enrichment downstream decide."""
    if not work_modes:
        return None
    return any(m in _REMOTE_WORK_MODES for m in work_modes)


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_lib.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10_000] or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
