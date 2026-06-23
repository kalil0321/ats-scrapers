"""DiDi Global (滴滴) careers scraper.

DiDi Chuxing is the world's leading mobile transportation platform,
operating in 18+ countries. Their public no-auth careers API at
``talent.didiglobal.com`` exposes the full catalogue across social and
campus recruitment.

    GET https://talent.didiglobal.com/recruit-portal-service/api/job/front/list
        ?recruitType={1|3}&page={N}&size={M}

The list endpoint serves a fixed ~16-item page server-side regardless
of the ``size`` parameter requested. ``recruitType=1`` is social
recruitment, ``recruitType=3`` is campus; we iterate both and dedup by
``jdId`` since DiDi mirrors many roles across both surfaces. The list
response carries enough structured metadata (jdNo, workArea, deptName,
jobName, refreshTime) for the canonical Job schema; the long-form
``jobDuty`` / ``jobQualification`` fields exist only on the per-jd
``view/{jdId}`` detail endpoint and are intentionally NOT fetched here
to keep this a single-pass scraper. Description-text enrichment can be
done out-of-band later.

Verified 2026-05-12: total=1217 live social postings.
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

API_URL = "https://talent.didiglobal.com/recruit-portal-service/api/job/front/list"
# The server caps each response at ~16 rows regardless of the size we
# request — keep this aligned so progress reporting and page-count
# math match what we actually receive.
DEFAULT_PAGE_SIZE = 16
DEFAULT_MAX_PAGES = 500
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# DiDi's own portal taxonomy. recruitType=1 is the social-hires
# surface (the bulk of the catalogue), recruitType=3 is campus.
# Iterating both and deduping by jdId matches what a candidate sees
# when toggling the tabs on the live site.
RECRUIT_TYPES: tuple[int, ...] = (1, 3)

# CJK range used to decide language. We don't try to differentiate
# Chinese vs. Japanese vs. Korean — DiDi's careers content is
# zh-cn or English in practice.
_CJK_RE = re.compile(r"[一-鿿]")

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://talent.didiglobal.com/social/",
}


@ScraperRegistry.register(ATSType.DIDI)
class DidiScraper(BaseScraper):
    """DiDi Global careers scraper — single-source for the whole company.

    ``company_slug`` is informational; the API is global and
    company-wide. Pagination follows the real param shape used by the
    portal (``recruitType`` / ``page`` / ``size``) rather than the
    aliases sometimes documented elsewhere.
    """

    ats = ATSType.DIDI

    def __init__(
        self,
        company_slug: str = "didi",
        *,
        timeout: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        recruit_types: tuple[int, ...] = RECRUIT_TYPES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.page_size = page_size
        self.max_pages = max_pages
        self.recruit_types = recruit_types

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        merged: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            for recruit_type in self.recruit_types:
                first = await self._fetch_page(
                    client, sem, recruit_type=recruit_type, page=1
                )
                data = first.get("data") or {}
                total = int(data.get("total") or 0)
                items = data.get("items") or []
                self._merge(items, recruit_type, seen, merged)
                if total <= self.page_size or len(items) < self.page_size:
                    continue

                page_count = min(
                    (total + self.page_size - 1) // self.page_size,
                    self.max_pages,
                )
                lock = asyncio.Lock()

                async def one(
                    page: int,
                    rt: int = recruit_type,
                    page_lock: asyncio.Lock = lock,
                ) -> None:
                    payload = await self._fetch_page(
                        client, sem, recruit_type=rt, page=page
                    )
                    page_items = (payload.get("data") or {}).get("items") or []
                    async with page_lock:
                        self._merge(page_items, rt, seen, merged)

                await asyncio.gather(
                    *(one(p) for p in range(2, page_count + 1))
                )
        return merged

    def _merge(
        self,
        items: list[dict[str, Any]],
        recruit_type: int,
        seen: set[str],
        out: list[Job],
    ) -> None:
        for item in items:
            job = self._parse_job(item, recruit_type=recruit_type)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            out.append(job)

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        recruit_type: int,
        page: int,
    ) -> dict[str, Any]:
        params = {
            "recruitType": recruit_type,
            "page": page,
            "size": self.page_size,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers=HEADERS
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"DiDi fetch failed at recruitType={recruit_type} "
                            f"page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"DiDi returned non-JSON at recruitType={recruit_type} "
                        f"page={page}: {exc}"
                    ) from exc
                meta = payload.get("meta") or {}
                # DiDi wraps app-level errors in meta.code (0 = success).
                if meta.get("code") not in (0, None):
                    raise ScraperError(
                        f"DiDi returned meta.code={meta.get('code')} "
                        f"({meta.get('message')!r}) at recruitType="
                        f"{recruit_type} page={page}"
                    )
                return payload
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"DiDi returned {response.status_code} at "
                        f"recruitType={recruit_type} page={page} after "
                        f"{MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"DiDi returned {response.status_code} at "
                f"recruitType={recruit_type} page={page}"
            )
        raise ScraperError(
            f"DiDi exhausted retries at recruitType={recruit_type} "
            f"page={page}: {last_exc}"
        )

    def _parse_job(
        self, item: dict[str, Any], *, recruit_type: int | None = None
    ) -> Job | None:
        # Prefer jdId (the portal's stable per-posting key); fall back
        # to id only if it's ever populated.
        ats_id_raw = item.get("jdId") if item.get("jdId") is not None else item.get("id")
        if ats_id_raw is None:
            return None
        ats_id = str(ats_id_raw)
        title = (item.get("jobName") or "").strip()
        if not ats_id or not title:
            return None

        description = _compose_description(
            item.get("jobDuty"), item.get("jobQualification")
        )
        posted_at = _parse_epoch_ms(item.get("createTime")) or _parse_refresh_time(
            item.get("refreshTime")
        )
        location = _clean_str(item.get("workArea"))
        country_iso = _infer_country_iso(location)
        language = "zh" if _CJK_RE.search(title) else "en"

        jd_no = _clean_str(item.get("jdNo"))
        requisition_id = jd_no or None

        # raw: keep the ATS-specific fields the canonical schema can't
        # represent. ``recruit_type`` here is the *surface* this row
        # was discovered on (1=social / 3=campus), which is more useful
        # than the per-item recruitType field (often null in list view).
        raw: dict[str, Any] = {}
        for source, dest in (
            ("labelCode", "label_codes"),
            ("labels", "labels"),
            ("isUrgent", "is_urgent"),
            ("channelId", "channel_id"),
            ("jobLevel", "job_level"),
        ):
            value = item.get(source)
            if value not in (None, "", []):
                raw[dest] = value
        rt_value = item.get("recruitType")
        if rt_value not in (None, ""):
            raw["recruit_type"] = rt_value
        elif recruit_type is not None:
            raw["recruit_type"] = recruit_type

        surface = "campus" if recruit_type == 3 else "social"

        return Job(
            url=f"https://talent.didiglobal.com/{surface}/p/{ats_id}",
            title=title,
            company="DiDi",
            ats_type=ATSType.DIDI,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            department=_clean_str(item.get("jobTypeName"))
            or _clean_str(item.get("jobType")),
            team=_clean_str(item.get("deptName")),
            requisition_id=requisition_id,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language=language,
            raw=raw or None,
        )


def _compose_description(*sources: object) -> str | None:
    """Concatenate ``jobDuty`` + ``jobQualification`` and cap at 10kB.

    Both fields are plain text from the DiDi backend, so no HTML
    stripping is needed — just trim, drop empties, and collapse runs
    of blank lines.
    """
    parts: list[str] = []
    for src in sources:
        if isinstance(src, str) and src.strip():
            parts.append(src.strip())
    if not parts:
        return None
    text = "\n\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:10_000] or None


def _clean_str(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_epoch_ms(value: object) -> datetime | None:
    """``createTime`` is documented as epoch milliseconds when present."""
    if value is None or value == "":
        return None
    try:
        # Tolerate both int and numeric str.
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_refresh_time(value: object) -> datetime | None:
    """``refreshTime`` is a server-local string like
    ``"2026-05-11 22:39:45"`` — used as a fallback when createTime
    is null in the list payload."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _infer_country_iso(location: str | None) -> str | None:
    """Cheap CJK→CN heuristic. DiDi's domestic catalogue uses the
    Chinese city name (``北京市``, ``上海市``), so any CJK in the
    location string almost always means a mainland-China posting.
    Foreign cities are left for the LLM enrichment pass downstream."""
    if not location:
        return None
    if _CJK_RE.search(location):
        return "CN"
    return None
