"""Avito Maroc (avito.ma) — Morocco's largest classifieds, jobs section.

Avito is the Schibsted-spun, OLX-style classifieds giant for Morocco.
Beyond the cars / real-estate / electronics verticals it runs an
``Emploi`` jobs section with ~800–1k active postings — mostly
informal-economy (call centres, retail, hospitality, drivers) but
also a growing share of corporate listings. Coverage is
complementary to Rekrute: where Rekrute leans recruiter-portal and
white-collar, Avito leans direct-employer and blue-collar.

Avito's front-end is a Next.js SPA at
``https://www.avito.ma/fr/maroc/emploi`` — every page ships its
server-side data embedded in a ``<script id="__NEXT_DATA__">`` blob
as JSON. Parsing that JSON is dramatically more robust than DOM
walking (Avito redesigns the visual layout frequently, the data
shape moves slowly). The pagination param is ``?o=N`` (1-based).

Important: the *non-restricted* ``/fr/maroc/offres_d_emploi`` URL
folds in "boosted" ads from other categories (electronics, real
estate, …) — we use ``/fr/maroc/emploi`` which restricts to the
``Emploi`` (cat 6200) parent.

JSON row shape (one entry in ``props.pageProps.componentProps.ads.ads``):

    {
      "id": "77081446",
      "listId": "57625044",
      "subject": "Femme de ménage, garde malade, nounours...",
      "description": "2TG business international est une société…",
      "category": {
        "formatted": "Emploi - Centre d'appels",
        "name": "Centre d'appels", "id": "6050",
        "parent": {"id": "6200", "name": "Emploi"}
      },
      "seller": {"id": "7373", "type": "STORE", "name": "2TG …"},
      "location": "Oujda, Bd Hassan II",
      "date": "il y a 7 minutes",
      "images": [...],
      "href": "https://www.avito.ma/fr/bd_hassan_ii/centre_d_appels/Femme_de_ménage_…_57625044.htm"
    }

We treat ``id`` as Avito's canonical ATS id (a numeric posting id).
``date`` is a relative French phrase — we parse it on a best-effort
basis to produce ``posted_at``; ambiguous values stay ``None`` so the
LLM enrichment downstream can fill in.

Single-source scraper: ``company_slug`` is informational and ignored.
All rows are emitted with ``country_iso="MA"`` and ``region="Africa"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

# --- URL constants ----------------------------------------------------

SITE_BASE = "https://www.avito.ma"
# ``/fr/maroc/emploi`` restricts to the Emploi parent category (6200).
# The alternate ``/fr/maroc/offres_d_emploi`` URL mixes in boosted ads
# from non-job categories — easy to mis-classify, so we avoid it.
LISTING_URL_TEMPLATE = "https://www.avito.ma/fr/maroc/emploi?o={page}"

# Cat 6200 is the ``Emploi`` parent that scopes the listing. We use
# it as a guard to drop the occasional ad that slips through.
JOBS_PARENT_CATEGORY_ID = "6200"
JOBS_PARENT_CATEGORY_NAME = "Emploi"

DEFAULT_MAX_PAGES = 100  # ~800 / 30 per page = ~27 normally.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# --- __NEXT_DATA__ parsing --------------------------------------------
#
# Avito embeds its server-rendered state in a single
# ``<script id="__NEXT_DATA__" type="application/json">…</script>``
# block. We slice it out with a tolerant regex and parse the JSON.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>',
    re.DOTALL,
)

# Relative French time phrases Avito serves on ``date``. We translate
# them to a posted-at timestamp anchored at fetch time. Avito doesn't
# expose an absolute timestamp on the listing endpoint so this is a
# best-effort signal — LLM enrichment downstream can refine if
# needed.
_REL_TIME_RE = re.compile(
    r"il\s+y\s+a\s+(\d+)\s+(minute|minutes|heure|heures|jour|jours|"
    r"semaine|semaines|mois|an|ans|année|années)",
    re.IGNORECASE,
)


@ScraperRegistry.register(ATSType.AVITOMA)
class AvitoMarocScraper(BaseScraper):
    """Avito.ma — Morocco's largest classifieds (jobs section).

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``max_pages``: safety cap. Real catalogue is ~30 pages; default
      100 leaves headroom.
    - ``concurrency``: parallel page fetches. Default 4 — Avito is
      generous but we stay polite.
    """

    ats = ATSType.AVITOMA

    def __init__(
        self,
        company_slug: str = "any",
        *,
        timeout: float = 60.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        self.concurrency = max(1, concurrency)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        fetched_at = datetime.now()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            return await self._walk_pages(client, fetched_at)

    async def _walk_pages(
        self, client: httpx.AsyncClient, fetched_at: datetime,
    ) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        sem = asyncio.Semaphore(self.concurrency)
        stop_at_page: dict[str, int | None] = {"value": None}

        async def fetch_page(page_no: int) -> list[Job]:
            async with sem:
                if (
                    stop_at_page["value"] is not None
                    and page_no >= stop_at_page["value"]
                ):
                    return []
                html_body = await self._get_listing_page(client, page_no)
            if html_body is None:
                return []
            ads = _extract_ads(html_body)
            if not ads:
                if stop_at_page["value"] is None or page_no < stop_at_page["value"]:
                    stop_at_page["value"] = page_no
                return []
            return [
                job
                for ad in ads
                if (job := _parse_ad(ad, fetched_at=fetched_at)) is not None
            ]

        page_no = 1
        while page_no <= self.max_pages:
            wave_end = min(page_no + self.concurrency, self.max_pages + 1)
            results = await asyncio.gather(
                *(fetch_page(p) for p in range(page_no, wave_end))
            )
            wave_had_rows = False
            for page_jobs in results:
                if page_jobs:
                    wave_had_rows = True
                for job in page_jobs:
                    if job.ats_id is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
            if not wave_had_rows or stop_at_page["value"] is not None:
                break
            page_no = wave_end
        log.info("Avito Maroc: fetched %d unique jobs", len(jobs))
        return jobs

    async def _get_listing_page(
        self, client: httpx.AsyncClient, page_no: int,
    ) -> str | None:
        """Fetch one ``/fr/maroc/emploi?o=N`` listing page. Returns the
        HTML body or ``None`` to halt the walk."""
        url = LISTING_URL_TEMPLATE.format(page=page_no)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(url, headers=_HEADERS)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Avito Maroc page=%d transport error after %d retries: %s",
                        page_no, MAX_RETRIES, exc,
                    )
                    return None
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            status = response.status_code
            if status == 200:
                return response.text
            if status in (429,) or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Avito Maroc page=%d returned %d after %d retries — stopping",
                        page_no, status, MAX_RETRIES,
                    )
                    return None
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** (attempt - 1))
                )
                await asyncio.sleep(delay)
                continue
            log.warning(
                "Avito Maroc page=%d returned %d — stopping pagination",
                page_no, status,
            )
            return None
        log.warning(
            "Avito Maroc page=%d exhausted retries: %s",
            page_no, last_exc,
        )
        return None


# --- module-level helpers ---------------------------------------------


def _extract_ads(html_body: str) -> list[dict[str, Any]]:
    """Pull the ad list from a page's ``__NEXT_DATA__`` blob.

    Returns ``[]`` when the blob is missing, malformed, or doesn't
    contain ads — pagination uses ``[]`` as the "stop here" signal.
    """
    m = _NEXT_DATA_RE.search(html_body)
    if m is None:
        return []
    try:
        data = json.loads(m.group("json"))
    except (ValueError, json.JSONDecodeError):
        log.warning("Avito Maroc: __NEXT_DATA__ JSON parse failed")
        return []
    try:
        ads = data["props"]["pageProps"]["componentProps"]["ads"]["ads"]
    except (KeyError, TypeError):
        log.warning("Avito Maroc: __NEXT_DATA__ shape changed — no ads list")
        return []
    if not isinstance(ads, list):
        return []
    return [a for a in ads if isinstance(a, dict)]


def _parse_ad(ad: dict[str, Any], *, fetched_at: datetime) -> Job | None:
    """Parse one ad dict into a Job. Returns ``None`` when:

    - the ad isn't in the ``Emploi`` parent category (a boosted ad
      from elsewhere — we drop rather than mis-classify);
    - the id / subject / href is missing.
    """
    if not _is_jobs_ad(ad):
        return None

    raw_id = ad.get("id") or ad.get("listId")
    if raw_id is None:
        return None
    ats_id = str(raw_id).strip()
    if not ats_id:
        return None

    title = (ad.get("subject") or "").strip()
    if not title:
        return None

    href = (ad.get("href") or "").strip()
    if not href:
        return None
    url = _absolute_url(href)

    seller = ad.get("seller") or {}
    company = (seller.get("name") or "").strip() or "Unknown"

    description = (ad.get("description") or "").strip() or None
    if description and len(description) > 5000:
        description = description[:5000].rstrip() + "…"

    location = (ad.get("location") or "").strip() or None

    category = ad.get("category") or {}
    category_name = (category.get("formatted") or category.get("name") or "").strip()
    department = category_name or None

    posted_at = _parse_relative_date(ad.get("date"), now=fetched_at)

    raw: dict[str, Any] = {}
    if seller.get("type"):
        raw["seller_type"] = seller["type"]
    if seller.get("id"):
        raw["seller_id"] = str(seller["id"])
    images = ad.get("images")
    if isinstance(images, list) and images:
        raw["image_count"] = len(images)
    list_id = ad.get("listId")
    if list_id and str(list_id) != ats_id:
        raw["list_id"] = str(list_id)
    if category.get("id"):
        raw["category_id"] = str(category["id"])
    if ad.get("isShop"):
        raw["is_shop"] = True
    if ad.get("isPremium"):
        raw["is_premium"] = True
    if ad.get("isUrgent"):
        raw["is_urgent"] = True

    return Job(
        url=url,
        title=title,
        company=company,
        ats_type=ATSType.AVITOMA,
        ats_id=ats_id,
        location=location,
        country_iso="MA",
        region="Africa",
        department=department,
        description=description,
        posted_at=posted_at,
        fetched_at=fetched_at,
        language="fr",
        raw=raw or None,
    )


def _is_jobs_ad(ad: dict[str, Any]) -> bool:
    """True when the ad belongs to the Avito ``Emploi`` parent
    category. Boosted / cross-promoted listings from other categories
    sometimes appear on the jobs URL; we filter them out so the
    dataset stays clean."""
    category = ad.get("category") or {}
    parent = category.get("parent") or {}
    parent_id = str(parent.get("id") or "")
    parent_name = (parent.get("name") or "").strip()
    cat_id = str(category.get("id") or "")
    if parent_id == JOBS_PARENT_CATEGORY_ID:
        return True
    if parent_name.lower() == JOBS_PARENT_CATEGORY_NAME.lower():
        return True
    # The parent itself could be the Emploi root on top-level ads.
    return cat_id == JOBS_PARENT_CATEGORY_ID


def _absolute_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return SITE_BASE + path_or_url


# Map French relative-time unit → timedelta-compatible kwarg.
_UNIT_TO_KW: dict[str, str] = {
    "minute": "minutes", "minutes": "minutes",
    "heure": "hours", "heures": "hours",
    "jour": "days", "jours": "days",
    "semaine": "weeks", "semaines": "weeks",
    # ``mois`` / years are approximated — Avito surfaces "il y a 2
    # mois" so we approximate at 30 days per month.
}


def _parse_relative_date(
    value: object, *, now: datetime,
) -> datetime | None:
    """Convert ``"il y a 7 minutes"`` / ``"il y a 2 jours"`` into a
    UTC-naive datetime relative to ``now``. Returns ``None`` for
    unrecognised phrases — better to leave the field empty than
    fabricate a wrong timestamp.

    Approximations:
      - 1 ``mois`` = 30 days
      - 1 ``an`` / ``année`` = 365 days

    These are coarse but consistent — consumers know Avito's relative
    timestamps are not minute-precise anyway.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    m = _REL_TIME_RE.search(text)
    if m is None:
        return None
    try:
        count = int(m.group(1))
    except (TypeError, ValueError):
        return None
    unit = m.group(2).lower()
    if unit in _UNIT_TO_KW:
        delta = timedelta(**{_UNIT_TO_KW[unit]: count})
    elif unit in ("mois",):
        delta = timedelta(days=30 * count)
    elif unit in ("an", "ans", "année", "années"):
        delta = timedelta(days=365 * count)
    else:
        return None
    return now - delta


__all__ = [
    "AvitoMarocScraper",
]
