"""Profession.hu — Hungary's dominant direct-posting job board (~50k live).

Profession.hu hosts ~18-50k active postings at any given time and is by
far the largest Hungarian-language job platform; companies post directly
(it's not a LinkedIn / Indeed aggregator). May 2026 audit had Hungary at
near-zero share of the dataset — this single source unlocks the country.

Scraping surface: the public listing pages at
``https://www.profession.hu/allasok/{page}`` serve a complete
``dataLayer.push({"event": "view_item_list", ...})`` block on every
response. That block carries every field we need (title, employer,
category, sub-category, employment-type normalized to English, salary
visibility flag, experience bucket, location-id) — no per-job detail
fetch is required for the canonical schema. The matching
``<li class="advertisement-result-list-item" data-prof-id="…" data-link="…">``
tag pairs each item with its detail-page URL. Pagination is a plain
trailing path segment (``/allasok/2``, ``/allasok/3`` …) with 20 rows
per page; the listing header advertises a total (e.g.
``18750 db``) we parse to size the pagination plan.

Notes / quirks:

  - The ``?page=N`` query-string variant returns page 1 regardless of
    ``N`` — only the ``/allasok/{N}`` path form actually advances.

  - ``location_id`` is a normalised Hungarian slug, e.g.
    ``Heves_megye,_Gyöngyös`` (county + city). The scraper turns the
    underscores into spaces but leaves the comma layout intact;
    downstream LLM enrichment normalises further.

  - The dataLayer's ``item_category3`` is pre-normalised to English
    (``"full time"``, ``"part time"``, ``"contract"``, ``"internship"``,
    ``"temporary"``). We map straight off that English string — no need
    to translate ``Teljes munkaidős`` / ``Részmunkaidős`` ourselves.

  - ``item_variant`` is the salary-visibility flag
    (``salary publicised`` / ``salary confidential``). The structured
    HUF range only appears on the detail page; with no per-job fetch we
    leave ``salary_currency`` / ``salary_min`` / ``salary_max`` null on
    the listing surface. The visibility flag is preserved in ``raw``.

  - Hungarian listings are in Hungarian, so ``language="hu"`` and
    ``country_iso="HU"`` are populated unconditionally.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

LISTING_URL_TEMPLATE = "https://www.profession.hu/allasok/{page}"
DETAIL_URL_TEMPLATE = "https://www.profession.hu/allas/{job_id}"
PER_PAGE = 20  # Hard-coded by the listing renderer.
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
# Profession.hu currently exposes ~18-25k jobs; 1 500 pages × 20 rows
# = 30k is well above the live ceiling. Lower via ``max_pages`` for
# smoke runs.
DEFAULT_MAX_PAGES = 1500

# The dataLayer push we rely on. Anchored to ``event":"view_item_list"``
# (with optional whitespace around the colon to tolerate the live
# minified shape and pretty-printed test fixtures alike) to avoid
# matching the unrelated ``view_item`` push on the detail page. The
# capture is everything up to the matching ``});`` — the JSON payload
# nests dicts so a naive ``\}`` match would stop at the first inner
# brace; we anchor on the closing ``);`` sequence instead and let the
# JSON parser deal with the contents.
_DATALAYER_RE = re.compile(
    r"dataLayer\.push\(\s*(\{\s*\"event\"\s*:\s*\"view_item_list\".*?\})\s*\)\s*;",
    re.DOTALL,
)
# Listing rows ship a ``data-prof-id`` + ``data-link`` pair on each
# ``<li class="advertisement-result-list-item">`` tag. We use the
# id→href map to turn dataLayer rows into absolute URLs.
_ROW_LINK_RE = re.compile(
    r'data-prof-id="(\d+)"\s+data-link="([^"]+)"'
)
# Total-postings counter in the listing header
# (``item_list_name`` field or visible header — both share the
# ``- {N} db -`` pattern; we read the dataLayer field which is more
# robust than the rendered DOM).
_TOTAL_RE = re.compile(r"-\s*(\d[\d\s ]*)\s*db\s*-")

# item_category3 → canonical EmploymentType. Source uses English
# strings even on the Hungarian-language site (verified live
# 2026-05-12 across pages 1, 5, 50, 200, 500).
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "full time": "FULL_TIME",
    "part time": "PART_TIME",
    "contract": "CONTRACT",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
    # Defensive: cover the Hungarian originals as well in case
    # Profession.hu ever stops normalising. ``Teljes munkaidős`` etc.
    # come from the rendered detail-page chip; not currently in the
    # dataLayer surface but cheap to support.
    "teljes munkaidős": "FULL_TIME",
    "részmunkaidős": "PART_TIME",
    "alkalmi munka": "TEMPORARY",
    "gyakornoki": "INTERN",
    "bedolgozói": "CONTRACT",
}


@ScraperRegistry.register(ATSType.PROFESSIONHU)
class ProfessionHuScraper(BaseScraper):
    """Profession.hu (Hungary) — direct-posting board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``).

    Knobs:
    - ``max_pages`` — pagination cap (default 1 500, comfortably above
      the ~1k pages × 20 rows the live board currently exposes).
    """

    ats = ATSType.PROFESSIONHU

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for job in items:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)  # type: ignore[arg-type]
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
            },
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Probe page 1 to learn the total job count.
            first_html = await self._fetch_page(client, sem, page=1)
            first_items, total = self._parse_listing(first_html)
            await absorb(first_items)

            if total <= PER_PAGE:
                return jobs

            page_count = min(
                (total + PER_PAGE - 1) // PER_PAGE, self.max_pages
            )
            if page_count <= 1:
                return jobs

            async def one(page: int) -> None:
                try:
                    body = await self._fetch_page(client, sem, page=page)
                except ScraperError as exc:
                    log.warning(
                        "profession.hu: page=%d failed: %s — skipping.",
                        page,
                        exc,
                    )
                    return
                items, _ = self._parse_listing(body)
                await absorb(items)

            await asyncio.gather(
                *(one(p) for p in range(2, page_count + 1))
            )
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> str:
        url = LISTING_URL_TEMPLATE.format(page=page)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"profession.hu fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"profession.hu returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"profession.hu returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"profession.hu exhausted retries at page={page}: {last_exc}"
        )

    def _parse_listing(self, html_body: str) -> tuple[list[Job], int]:
        """Extract jobs + total-row count from a listing-page HTML body.

        Returns ``([], 0)`` (rather than raising) when the dataLayer
        block is missing — that happens on empty / 404-style pages past
        the real pagination cap, and we'd rather drop a stray page than
        crash the whole scrape.
        """
        match = _DATALAYER_RE.search(html_body)
        if match is None:
            return [], 0
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            log.warning(
                "profession.hu: malformed view_item_list dataLayer: %s",
                exc,
            )
            return [], 0

        items = (
            (payload.get("ecommerce") or {}).get("items") or []
            if isinstance(payload.get("ecommerce"), dict)
            else []
        )

        # Build id → detail-URL map from the matching <li> tags. The
        # dataLayer row carries the integer ``item_id``, the <li> ships
        # the absolute URL; we join on stringified id.
        link_map = dict(_ROW_LINK_RE.findall(html_body))

        # Total job count for the active filter set — published in
        # ``item_list_name`` ("Állások, munkák ... - 18750 db - …").
        # Falls back to the raw item count if the header changes shape.
        total = 0
        list_name = ""
        for it in items:
            if isinstance(it, dict) and it.get("item_list_name"):
                list_name = str(it["item_list_name"])
                break
        m_total = _TOTAL_RE.search(list_name)
        if m_total:
            raw = re.sub(r"[\s ]", "", m_total.group(1))
            try:
                total = int(raw)
            except ValueError:
                total = 0

        jobs: list[Job] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            job = self._parse_item(raw_item, link_map)
            if job is not None:
                jobs.append(job)

        # Defensive: when the header's total is unparseable, at least
        # return the visible row count so single-page scraping still
        # works.
        if not total:
            total = len(jobs)
        return jobs, total

    def _parse_item(
        self, item: dict[str, Any], link_map: dict[str, str]
    ) -> Job | None:
        raw_id = item.get("item_id")
        if raw_id is None:
            return None
        ats_id = str(raw_id).strip()
        if not ats_id:
            return None

        title = (item.get("item_name") or "").strip()
        if not title:
            return None

        # ``affiliation`` is the hiring employer's display name;
        # ``item_brand`` is always "classified listing" (the product
        # tier), not the company — don't confuse the two.
        company = (item.get("affiliation") or "").strip() or "Unknown"

        # Prefer the canonical detail URL we scraped off the <li>; if
        # that's missing (rare — happens when a row is rendered without
        # the standard advertisement wrapper) fall back to the slug-less
        # ``/allas/{id}`` form, which Profession.hu 301-redirects to the
        # full slug.
        url = link_map.get(ats_id) or DETAIL_URL_TEMPLATE.format(
            job_id=ats_id
        )

        location_id = (item.get("location_id") or "").strip()
        location = _format_location(location_id)

        category3 = (item.get("item_category3") or "").strip().lower()
        employment_type = _EMPLOYMENT_TYPE_MAP.get(category3)

        salary_summary, salary_currency = _parse_salary_flag(
            (item.get("item_variant") or "").strip().lower()
        )

        raw: dict[str, object] = {}
        if item.get("item_category"):
            raw["category"] = item["item_category"]
        if item.get("item_category2"):
            raw["industry"] = item["item_category2"]
        if item.get("item_category4"):
            raw["experience_bucket"] = item["item_category4"]
        if item.get("item_variant"):
            raw["salary_visibility"] = item["item_variant"]
        if item.get("application_type"):
            raw["application_type"] = item["application_type"]
        if item.get("prof_product_name"):
            raw["modality"] = item["prof_product_name"]

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.PROFESSIONHU,
            ats_id=ats_id,
            location=location,
            country_iso="HU",
            region="Europe",
            language="hu",
            employment_type=employment_type,  # type: ignore[arg-type]
            salary_currency=salary_currency,
            salary_summary=salary_summary,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _format_location(location_id: str) -> str | None:
    """Profession.hu encodes ``location_id`` as
    ``County_megye,_City`` with underscore-spaces (e.g.
    ``Heves_megye,_Gyöngyös``). Empty strings show up for remote /
    country-wide postings — return None so downstream enrichment can
    apply its own heuristics.

    We swap underscores for spaces but leave the comma + ``megye``
    suffix intact; that's the canonical Hungarian county notation and
    matches how downstream geocoders / LLM enrichment expect to see
    Hungarian addresses.
    """
    if not location_id:
        return None
    return location_id.replace("_", " ").strip() or None


def _parse_salary_flag(variant: str) -> tuple[str | None, str | None]:
    """The listing dataLayer's ``item_variant`` is a salary-visibility
    flag, not a numeric range. We surface the human-readable label as
    ``salary_summary`` and pin the currency to HUF when the salary is
    publicly visible — the actual numeric range only appears on the
    detail page, and the scraper deliberately stays at listing level.

    Returns ``(summary, currency)``:
      - ``("Sávos bérezés", "HUF")`` when the listing says
        ``salary publicised``.
      - ``(None, None)`` for ``salary confidential`` and unknown
        flags — leave the field blank rather than misleading consumers
        with "confidential" as the salary.
    """
    if variant == "salary publicised":
        # Hungarian: "Sávos bérezés" = banded/range salary. Mirrors how
        # the rendered chip on the live site labels it.
        return "Sávos bérezés", "HUF"
    return None, None
