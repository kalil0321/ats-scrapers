"""Wantedly (https://www.wantedly.com) — Japan-focused direct-employer scraper.

Wantedly is the largest direct-posting startup / tech job platform in
Japan with ~158k live ``projects`` (their term for job postings). Companies
post directly through Wantedly's recruiting product — it is not an
aggregator of LinkedIn / Indeed feeds.

Public JSON API at ``https://www.wantedly.com/api/v1/projects`` — no auth,
no key. The endpoint requires ``X-Requested-With: XMLHttpRequest`` to
return JSON; without it Wantedly serves the HTML SSR page instead. The
``_metadata`` block on the first page exposes ``total_objects`` and
``total_pages`` for stable termination.

Pagination is page-numbered (one-indexed) via ``page=N&per_page=10``. The
API hard-caps ``per_page`` at 10 — any larger value is silently rejected
and 10 items are returned. We cap the default crawl at 500 pages
(~5k jobs) so ad-hoc runs stay bounded; pass ``max_pages`` to lift the
cap and walk the whole dataset.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the usajobs / mycareersfuture / wanted pattern). Output rows
carry the publishing employer's name as ``company`` so the publisher's
cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://www.wantedly.com/api/v1/projects"
PROJECT_URL_TEMPLATE = "https://www.wantedly.com/projects/{id}"
PAGE_SIZE = 10  # API hard-caps at 10; larger values are silently clamped.
DEFAULT_MAX_PAGES = 500  # ~5k jobs by default; bump via ``max_pages``.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
MAX_DESCRIPTION_LEN = 10_000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# CJK Unified Ideographs + Hiragana + Katakana + Halfwidth Katakana ranges.
# Used as a quick "is this a Japanese listing?" heuristic on the title.
_CJK_RE = re.compile(
    r"[　-〿"   # CJK symbols & punctuation
    r"぀-ゟ"    # Hiragana
    r"゠-ヿ"    # Katakana
    r"㐀-䶿"    # CJK Unified Ideographs Ext A
    r"一-鿿"    # CJK Unified Ideographs
    r"ｦ-ﾟ"    # Halfwidth Katakana
    r"]"
)

# Standalone tokens in ``location`` that signal the role is clearly
# outside Japan. The default ``country_iso`` is ``"JP"`` — when any of
# these match (case-insensitive whole token), we drop back to ``None``
# so downstream enrichment can resolve the actual country. Mixed bag of
# ISO-3166 alpha-2 / alpha-3 codes and common English country / city
# names that show up on Wantedly's growing global postings.
_NON_JP_HINTS = frozenset({
    # Country codes. Ambiguous 2-letter ISO codes that collide with common
    # English words (AT, IN, IT, NO, CO, ID, PE) are intentionally omitted —
    # they would wrongly null the country on valid JP postings; their full
    # country names below still cover the unambiguous cases.
    "US", "USA", "UK", "GB", "FR", "DE", "ES", "PT", "NL", "BE",
    "CH", "SE", "DK", "FI", "PL", "CZ", "GR", "IE",
    "CA", "MX", "BR", "AR", "CL",
    "CN", "HK", "TW", "KR", "SG", "MY", "TH", "VN", "PH",
    "AU", "NZ", "ZA", "AE", "SA", "IL", "TR",
    # Common country names (English).
    "SINGAPORE", "TAIWAN", "KOREA", "CHINA", "HONG", "KONG", "VIETNAM",
    "THAILAND", "MALAYSIA", "INDONESIA", "PHILIPPINES", "INDIA",
    "AUSTRALIA", "CANADA", "MEXICO", "BRAZIL", "FRANCE", "GERMANY",
    "SPAIN", "ITALY", "PORTUGAL", "NETHERLANDS", "BELGIUM", "SWITZERLAND",
    "AUSTRIA", "SWEDEN", "NORWAY", "DENMARK", "FINLAND", "POLAND",
    "IRELAND", "GREECE", "TURKEY", "ISRAEL",
})


@ScraperRegistry.register(ATSType.WANTEDLY)
class WantedlyScraper(BaseScraper):
    """Wantedly (wantedly.com) — Japan-focused direct-employer postings.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper enumerates the full ``/projects``
    feed across all employers.

    Default crawl stops at ``max_pages`` pages (500 = 5,000 jobs). Pass
    a larger value (or ``None``) to walk the entire dataset.
    """

    ats = ATSType.WANTEDLY

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int | None = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # First page is fetched serially so we can read ``total_pages``
            # from ``_metadata`` and plan the rest of the walk.
            first = await self._fetch_page(client, sem, page=1)
            await self._absorb(first.get("data") or [], seen, jobs, lock)

            total_pages = self._total_pages(first)
            if self.max_pages is not None:
                total_pages = min(total_pages, self.max_pages)
            if total_pages <= 1:
                return jobs

            async def worker(page: int) -> None:
                payload = await self._fetch_page(client, sem, page=page)
                await self._absorb(payload.get("data") or [], seen, jobs, lock)

            await asyncio.gather(*(worker(p) for p in range(2, total_pages + 1)))

        return jobs

    @staticmethod
    def _total_pages(payload: dict[str, Any]) -> int:
        meta = payload.get("_metadata") or {}
        if not isinstance(meta, dict):
            return 1
        total = meta.get("total_pages")
        if isinstance(total, int) and total > 0:
            return total
        # Fallback: derive from total_objects when total_pages is absent.
        total_objects = meta.get("total_objects")
        if isinstance(total_objects, int) and total_objects > 0:
            return (total_objects + PAGE_SIZE - 1) // PAGE_SIZE
        return 1

    async def _absorb(
        self,
        items: list[dict[str, Any]],
        seen: set[str],
        jobs: list[Job],
        lock: asyncio.Lock,
    ) -> None:
        async with lock:
            for item in items:
                job = self._parse_job(item)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            # Required — without this header Wantedly returns the HTML SSR
            # page (200 OK) instead of the JSON document.
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0",
        }
        params = {
            "per_page": PAGE_SIZE,
            "page": page,
            "hiring": "true",
            "order": "published_at",
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(API_URL, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Wantedly fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Wantedly returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 404:
                # A 404 on the very first page is not "past the end" — it
                # signals a geo-block or a changed API path. Raise so the
                # run fails loudly instead of returning a silent empty slice.
                if page == 1:
                    raise ScraperError(
                        "Wantedly returned 404 at page=1 "
                        "(geo-block or API path change?)"
                    )
                # Past the end of the dataset — treat as empty slice.
                return {"data": [], "_metadata": {}}
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Wantedly returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Wantedly returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"Wantedly exhausted retries at page={page}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, project: dict[str, Any]) -> Job | None:
        raw_id = project.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id).strip()
        title = (project.get("title") or "").strip()
        if not ats_id or not title:
            return None

        company_obj = project.get("company") or {}
        if not isinstance(company_obj, dict):
            company_obj = {}
        company_name = (company_obj.get("name") or "").strip() or "Unknown"
        # Wantedly's company object historically exposed ``slug``; the
        # current v1 listing serializer ships ``id`` instead. Try both
        # so a future schema bump that re-adds ``slug`` lands gracefully.
        company_slug = company_obj.get("slug")
        if not isinstance(company_slug, str) or not company_slug:
            company_slug = None

        location = _format_location(
            project.get("location"), project.get("location_suffix")
        )
        country_iso = _infer_country_iso(location)
        language = "ja" if _CJK_RE.search(title) else "en"

        # Wantedly splits the body across ``description`` (overview) and
        # ``looking_for`` (the "who we want" pitch). Concatenate so downstream
        # text-mining sees one document.
        description = _build_description(
            project.get("description"), project.get("looking_for")
        )

        tags = project.get("tags") or []
        tag_names: list[str] = []
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict):
                    name = tag.get("name")
                    if isinstance(name, str) and name.strip():
                        tag_names.append(name.strip())
        department = tag_names[0] if tag_names else None

        raw: dict[str, Any] = {
            "company_slug": company_slug,
            "tags": tag_names[:10],
            "support_count": project.get("support_count"),
            "page_view": project.get("page_view"),
            "candidate_count": project.get("candidate_count"),
        }

        return Job(
            url=PROJECT_URL_TEMPLATE.format(id=ats_id),
            title=title,
            company=company_name,
            ats_type=ATSType.WANTEDLY,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            description=description,
            department=department,
            language=language,
            posted_at=_parse_iso(project.get("published_at")),
            fetched_at=datetime.now(),
            raw=raw,
        )


def _format_location(location: object, suffix: object) -> str | None:
    primary = location.strip() if isinstance(location, str) else ""
    secondary = suffix.strip() if isinstance(suffix, str) else ""
    if primary and secondary:
        return f"{primary}, {secondary}"
    return primary or secondary or None


def _infer_country_iso(location: str | None) -> str | None:
    """Default to ``JP`` — Wantedly is overwhelmingly Japanese — unless
    ``location`` carries an obvious non-Japanese country hint.

    Cheap and intentionally conservative: any CJK character anywhere in
    the string keeps the JP default; otherwise we look for a standalone
    non-JP country/region token (``"US"``, ``"Singapore"``, …) and bail
    to ``None`` so downstream enrichment can resolve the actual country.
    """
    if not location:
        return "JP"
    if _CJK_RE.search(location):
        return "JP"
    # Tokenize on non-alphanumerics and check each token.
    tokens = {t.upper() for t in re.findall(r"[A-Za-z]+", location)}
    if tokens & _NON_JP_HINTS:
        return None
    return "JP"


def _build_description(description: object, looking_for: object) -> str | None:
    parts: list[str] = []
    for piece in (description, looking_for):
        text = _html_to_text(piece)
        if text:
            parts.append(text)
    if not parts:
        return None
    joined = "\n\n".join(parts)
    return joined[:MAX_DESCRIPTION_LEN]


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = _HTML_TAG_RE.sub(" ", value)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
