"""Programathor (https://programathor.com.br) — Brazilian tech job board.

Programathor is the largest direct-posting tech job board in Brazil
(~3,000 active recent postings, 15,000+ historical). Companies post
directly through Programathor — not syndicated from LinkedIn or Indeed.

The site geo-blocks non-Brazilian IPs (returns 403). The library
itself doesn't ship a proxy — running from a Brazilian residential
IP works without one. Servers in US/EU clouds need to route through
a residential proxy: pass ``proxy_url`` to the constructor or set
the ``PROXY`` env variable. Both standard
``http://user:pass@host:port`` URLs and the 4-colon
``host:port:user:pass`` shape some providers ship are accepted.

Pagination is HTML-only (no JSON API) — 15 jobs per page on
``/jobs?page=N``. The listing card carries enough fields that we
don't need per-job detail fetches:

  <h3>Title</h3>
  <span><i class="fa fa-briefcase"></i>Company</span>
  <span><i class="fas fa-map-marker-alt"></i>Location</span>
  <span><i class="fa fa-building"></i>Company size</span>
  <span><i class="far fa-money-bill-alt"></i>Salary range</span>
  <span><i class="far fa-chart-bar"></i>Seniority</span>
  <span><i class="far fa-file-alt"></i>Contract type (CLT/PJ/Estágio)</span>
  <span class="tag-list ...">Skill tag</span>  (multiple)

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://programathor.com.br"
# 15 jobs/page is the site default; non-configurable. The site keeps
# a long tail of historical postings reachable via deep pagination
# (id 9000+ at page=1000) but listings beyond ~200 pages drift into
# stale roles. Cap at 200 by default → ~3,000 most-recent active jobs.
DEFAULT_MAX_PAGES = 200
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0
DETAIL_CONCURRENCY = 4

_TAG_RE = re.compile(r"<[^>]+>")
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_DETAIL_DESCRIPTION_RE = re.compile(
    r'<(?:div|section|article)[^>]+class=["\'][^"\']*(?:job-description|description|job-detail)[^"\']*["\'][^>]*>(?P<body>.*?)</(?:div|section|article)>',
    re.IGNORECASE | re.DOTALL,
)
_JOB_LINK_RE = re.compile(r'href="(/jobs/(?P<id>\d+)-[a-z0-9-]+)"')
# Each card is wrapped in `<div class="cell-list ">…</div>` containing
# one anchor with the job link. We anchor the regex on the cell start
# so we can scope per-card field extraction without leaking across
# cards.
_CARD_RE = re.compile(
    r'<div class="cell-list[^"]*">\s*<a[^>]+href="(/jobs/(?P<id>\d+)-[^"]+)"\s*>(?P<body>.*?)</a>\s*</div>',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<h3[^>]*>(?P<t>.*?)(?:<span[^>]*>NOVA</span>)?</h3>', re.DOTALL)
_BRIEFCASE_RE = re.compile(r"<i[^>]+fa-briefcase[^>]*>\s*</i>(?P<v>[^<]+)")
_LOCATION_RE = re.compile(r"<i[^>]+fa-map-marker-alt[^>]*>\s*</i>(?P<v>[^<]+)")
_COMPANY_TYPE_RE = re.compile(r"<i[^>]+fa-building[^>]*>\s*</i>(?P<v>[^<]+)")
_SALARY_RE = re.compile(r"<i[^>]+fa-money-bill-alt[^>]*>\s*</i>(?P<v>[^<]+)")
_CONTRACT_RE = re.compile(r"<i[^>]+fa-file-alt[^>]*>\s*</i>(?P<v>[^<]+)")
_SKILL_TAG_RE = re.compile(r"<span class='tag-list[^']*'>([^<]+)</span>")

# Contract type labels → canonical EmploymentType.
_EMPLOYMENT_MAP: dict[str, str] = {
    "clt": "FULL_TIME",
    "pj": "CONTRACT",
    "estágio": "INTERN",
    "estagio": "INTERN",
    "freelance": "CONTRACT",
    "temporário": "TEMPORARY",
    "temporario": "TEMPORARY",
}


def _resolve_proxy_url(raw: str | None) -> str | None:
    """Accept the 4-colon ``host:port:user:pass`` shape some
    residential-proxy providers ship and convert to the standard
    ``http://user:pass@host:port`` URL httpx expects. Plain
    ``http(s)://…`` URLs pass through.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^http://([^:/@]+):(\d+):([^:]+):(.+)$", raw)
    if m:
        host, port, user, pw = m.groups()
        return f"http://{user}:{pw}@{host}:{port}"
    return raw


@ScraperRegistry.register(ATSType.PROGRAMATHOR)
class ProgramathorScraper(BaseScraper):
    """Programathor (programathor.com.br) — Brazilian tech jobs.

    Single-source: ``company_slug`` is ignored. Pass anything (``"any"``,
    ``""``) — the scraper paginates the entire jobs board.

    Knobs:
    - ``proxy_url`` — explicit proxy URL. Falls back to ``PROXY`` env
      var (4-colon ``host:port:user:pass`` shape auto-converted),
      then to direct connection. Most users running locally don't
      need one; servers on US/EU clouds will hit Programathor's
      403 geo-block without one.
    - ``max_pages`` — pagination cap (default 200 → ~3,000 jobs).
    """

    ats = ATSType.PROGRAMATHOR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        proxy_url: str | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.proxy_url = _resolve_proxy_url(proxy_url) or _resolve_proxy_url(
            os.environ.get("PROXY")
        )
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            client_kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy_url:
                client_kwargs["proxy"] = self.proxy_url
                client_kwargs["verify"] = False
            async with httpx.AsyncClient(**client_kwargs) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(client, sem, copy)
            return copy.description

        return asyncio.run(run())

    async def _fetch_async(self) -> list[Job]:
        seen_ids: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for j in items:
                    if j.ats_id in seen_ids:
                        continue
                    seen_ids.add(j.ats_id)
                    jobs.append(j)

        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
            # Some residential-proxy providers terminate TLS with a
            # CA chain that isn't always in the system trust store;
            # the requests carry no PII so disabling verify is
            # acceptable here. (httpx warns once.)
            client_kwargs["verify"] = False

        async with httpx.AsyncClient(**client_kwargs) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Find the highest page that still returns content. Pages
            # past the live tail return zero job cards rather than 404,
            # so we walk until we see a duplicate-only page (every id
            # already seen) or hit ``max_pages``.
            consecutive_empty = 0
            page = 1
            while page <= self.max_pages and consecutive_empty < 3:
                page_jobs = await self._fetch_page(client, sem, page)
                new_count = sum(1 for j in page_jobs if j.ats_id not in seen_ids)
                await absorb(page_jobs)
                if new_count == 0:
                    consecutive_empty += 1
                else:
                    consecutive_empty = 0
                page += 1
            if self.include_descriptions and jobs:
                detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_description(client, detail_sem, job) for job in jobs
                ))
        return jobs

    async def _enrich_description(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        try:
            text = await self._request_html(client, sem, str(job.url))
        except ScraperError:
            return
        description = _extract_description(text)
        if description and not job.description:
            job.description = description[:25_000]

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> list[Job]:
        url = f"{API_ROOT}/jobs?page={page}"
        text = await self._request_html(client, sem, url)
        return list(self._parse_listing(text))

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,*/*",
                            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Programathor fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 403:
                raise ScraperError(
                    f"Programathor returned 403 for {url} — site geo-blocks "
                    "non-Brazilian IPs; set the PROXY env variable or pass "
                    "proxy_url to the scraper"
                )
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Programathor returned {response.status_code} for "
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
                f"Programathor returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"Programathor exhausted retries for {url}: {last_exc}"
        )

    def _parse_listing(self, text: str):
        for card in _CARD_RE.finditer(text):
            ats_id = card.group("id")
            href = card.group(1)
            body = card.group("body")
            job = self._parse_card(ats_id=ats_id, href=href, body=body)
            if job is not None:
                yield job

    def _parse_card(self, *, ats_id: str, href: str, body: str) -> Job | None:
        title_match = _TITLE_RE.search(body)
        if not title_match:
            return None
        # Strip any nested NOVA / new-label spans before cleaning.
        title = _strip_html(title_match.group("t"))
        title = re.sub(r"\s*NOVA\s*$", "", title).strip()
        if not title:
            return None

        company = _strip_html(_extract(body, _BRIEFCASE_RE)) or "Unknown"
        location_raw = _extract(body, _LOCATION_RE)
        location = _normalize_location(location_raw)
        company_type = _strip_html(_extract(body, _COMPANY_TYPE_RE))
        salary_raw = _strip_html(_extract(body, _SALARY_RE))
        contract_raw = _strip_html(_extract(body, _CONTRACT_RE))

        employment_type = _EMPLOYMENT_MAP.get(contract_raw.lower()) if contract_raw else None
        commitment = contract_raw or None

        # Salary parsing — Programathor uses "Até R$5.000", "R$3.000 - R$5.000",
        # "A combinar". Capture min/max when explicit, currency BRL when present.
        salary_min, salary_max, salary_currency = _parse_salary(salary_raw)

        # Skill tags (each in its own span)
        skills = [
            _strip_html(t).strip()
            for t in _SKILL_TAG_RE.findall(body)
        ]
        skills = [s for s in skills if s]

        # Remote detection (Programathor uses the literal "Remoto")
        is_remote = bool(location_raw and "remoto" in location_raw.lower())

        raw: dict[str, Any] = {}
        if skills:
            raw["skills"] = skills[:30]
        if company_type:
            raw["company_type"] = company_type
        if salary_raw and not (salary_min or salary_max):
            raw["salary_text"] = salary_raw

        return Job(
            url=f"{API_ROOT}{href}",
            title=title,
            company=company,
            ats_type=ATSType.PROGRAMATHOR,
            ats_id=ats_id,
            location=location,
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period="MONTH",
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,
            commitment=commitment,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# --- module-level helpers ---------------------------------------------------


def _extract(body: str, pattern: re.Pattern) -> str:
    match = pattern.search(body)
    return match.group("v").strip() if match else ""


def _strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_description(text: str) -> str | None:
    match = _DETAIL_DESCRIPTION_RE.search(text)
    if match:
        cleaned = _strip_html(match.group("body"))
        if cleaned:
            return cleaned
    meta = _extract_meta_description(text)
    return meta or None


def _extract_meta_description(text: str) -> str | None:
    for tag in _META_TAG_RE.finditer(text):
        attrs = {
            m.group("name").lower(): html.unescape(m.group("value"))
            for m in _ATTR_RE.finditer(tag.group("attrs"))
        }
        kind = (attrs.get("name") or attrs.get("property") or "").lower()
        if kind not in {"description", "og:description"}:
            continue
        cleaned = _strip_html(attrs.get("content") or "")
        if cleaned:
            return cleaned
    return None


def _normalize_location(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.lower() == "remoto":
        return "Remote, Brazil"
    return f"{raw}, Brazil" if raw else None


_BRL_NUMBER_RE = re.compile(r"R\$\s*([\d.,]+)")


def _parse_salary(raw: str) -> tuple[float | None, float | None, str | None]:
    """Programathor salary strings:

    - "Até R$5.000"            → max=5000, BRL/month
    - "R$3.000 - R$5.000"     → min=3000, max=5000, BRL/month
    - "A combinar"             → no signal
    - "" / None                 → no signal
    """
    if not raw:
        return None, None, None
    nums = _BRL_NUMBER_RE.findall(raw)
    if not nums:
        return None, None, None
    parsed = [_parse_brl_amount(n) for n in nums]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return None, None, None
    if len(parsed) == 1:
        # "Até X" → only max; "A partir de X" → only min. Default
        # interpretation is "up to" since that's the common shape.
        if "até" in raw.lower() or "ate" in raw.lower():
            return None, parsed[0], "BRL"
        if "partir" in raw.lower():
            return parsed[0], None, "BRL"
        return None, parsed[0], "BRL"
    return parsed[0], parsed[-1], "BRL"


def _parse_brl_amount(raw: str) -> float | None:
    """``R$3.000`` → 3000.0; ``R$3.500,50`` → 3500.50.

    Brazilian currency uses ``.`` as the thousand separator and ``,``
    as the decimal point, opposite to en-US conventions.
    """
    if not raw:
        return None
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None
