"""Saramin (https://www.saramin.co.kr) — Korea's largest job board.

Saramin (사람인) is the largest direct-posting job platform in Korea —
competing head-to-head with JobKorea, with hundreds of thousands of
live postings spanning every industry, not just tech. Companies post
directly through Saramin's recruiting product.

There is no public JSON API: the search page is **server-rendered
HTML**. We GET the integrated-search endpoint and pull listing cards
out of the response body via BeautifulSoup. Pagination is offset-style
through ``recruitPage=N`` with ``recruitPageCount=40`` per page; the
server hard-caps page numbers around 99 so even a high-recall keyword
returns at most ~4,000 cards per sweep.

Single-source scraper: ``company_slug`` is reused as the *searchword*
so callers can target a vertical (``"developer"``, ``"디자이너"``,
``"마케팅"`` …). Passing ``"any"`` / empty falls back to no keyword —
the search engine then surfaces only the sponsored TOP100 banner cards
(~20 per page, repeating), so meaningful coverage requires picking a
keyword.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.saramin.co.kr"
SEARCH_PATH = "/zf_user/search"
JOB_URL_TEMPLATE = (
    "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx={rec_idx}"
)
PER_PAGE = 40  # The site honours pageCount=40 — larger values fall back to 40.
# Saramin caps ``recruitPage`` server-side around 99. Use 99 as both the
# explicit safety bound and the natural exit for keyword sweeps; lower
# this only if a specific caller wants to stop early.
MAX_PAGES = 99
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Empty searchword on Saramin renders a "noresult" banner with sponsored
# TOP100 cards repeating across pages. We use the same default sentinel
# as wanted / getonbrd so single-source scrapers stay swappable.
_EMPTY_SLUGS = {"", "any", "all", "saramin"}

# Korean employment-type labels that show up in ``<div class="job_condition">``
# → canonical EmploymentType enum value. The site frequently emits combo
# strings like ``정규직·계약직`` (full-time OR contract); we prefer the
# more permanent classification when both are present.
#
# - 정규직   : permanent / full-time
# - 계약직   : fixed-term contract
# - 인턴     : intern
# - 인턴직   : intern
# - 파견직   : dispatched/agency worker → CONTRACT
# - 프리랜서 : freelance → CONTRACT
# - 아르바이트 : part-time / hourly → PART_TIME
# - 파트타임 : part-time
# - 위촉직   : commissioned (sales agent contract) → CONTRACT
# - 일용직   : day-laborer / temporary → TEMPORARY
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "정규직": "FULL_TIME",
    "계약직": "CONTRACT",
    "인턴": "INTERN",
    "인턴직": "INTERN",
    "파견직": "CONTRACT",
    "프리랜서": "CONTRACT",
    "아르바이트": "PART_TIME",
    "파트타임": "PART_TIME",
    "위촉직": "CONTRACT",
    "일용직": "TEMPORARY",
}
# Precedence: when a card lists multiple types (정규직·계약직), pick the
# one earliest in this tuple. Permanent beats contract beats part-time.
_EMPLOYMENT_TYPE_PRIORITY = (
    "FULL_TIME", "INTERN", "CONTRACT", "PART_TIME", "TEMPORARY",
)

# ``등록일 YY/MM/DD`` → datetime. The site uses 2-digit years; we assume
# 2000+. ``수정일`` (modified-date) uses the same shape; we kept the
# original ``등록일`` (posted-date) where available and fall back to
# ``수정일`` only because most cards don't carry the former.
_POSTED_RE = re.compile(r"등록일\s*(\d{2})/(\d{2})/(\d{2})")
_MODIFIED_RE = re.compile(r"수정일\s*(\d{2})/(\d{2})/(\d{2})")

_REC_IDX_RE = re.compile(r"rec_idx=(\d+)")


@ScraperRegistry.register(ATSType.SARAMIN)
class SaraminScraper(BaseScraper):
    """Saramin (saramin.co.kr) — Korea's largest job platform.

    Single-source scraper: ``company_slug`` is used as the
    ``searchword`` query param so callers can scope sweeps to a vertical
    (e.g. ``SaraminScraper("개발자")`` for developer postings, or
    ``SaraminScraper("디자이너")`` for designer roles). Pass ``"any"``
    / ``""`` for an unscoped run (yields only the sponsored TOP100
    cards — coverage is intentionally narrow for that path).
    """

    ats = ATSType.SARAMIN

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        # Clamp at the server-side ceiling — passing 500 here would just
        # generate 400 wasted 200-OK-empty fetches.
        self.max_pages = max(1, min(max_pages, MAX_PAGES))
        self._searchword = (
            "" if company_slug.strip().lower() in _EMPTY_SLUGS
            else company_slug.strip()
        )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()
        # Once two pages in a row come back empty we treat the sweep as
        # exhausted — the site sometimes hands back a 200 with zero cards
        # for transient reasons, but two consecutive empties is conclusive.
        stop_event = asyncio.Event()

        async def absorb(page_jobs: list[Job]) -> int:
            added = 0
            async with lock:
                for job in page_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    added += 1
            return added

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            # Walk pages sequentially: we need to detect the empty-tail
            # boundary, and Saramin doesn't ship a total-pages hint we
            # can dispatch to. Sequential keeps the request rate gentle.
            consecutive_empty = 0
            for page in range(1, self.max_pages + 1):
                if stop_event.is_set():
                    break
                page_html = await self._request_html(client, sem, page)
                page_jobs = self._parse_page(page_html)
                if not page_jobs:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                    continue
                consecutive_empty = 0
                await absorb(page_jobs)
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    def _build_url(self, page: int) -> str:
        searchword = quote_plus(self._searchword)
        return (
            f"{API_ROOT}{SEARCH_PATH}?searchword={searchword}"
            f"&recruitPage={page}&recruitSort=relation&recruitPageCount={PER_PAGE}"
        )

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        page: int,
    ) -> str:
        url = self._build_url(page)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36"
                            ),
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Saramin fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Saramin returned {response.status_code} for "
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
                f"Saramin returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"Saramin exhausted retries for {url}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_page(self, html_text: str) -> list[Job]:
        soup = BeautifulSoup(html_text, "html.parser")
        cards = soup.select("div.item_recruit")
        out: list[Job] = []
        for card in cards:
            job = self._parse_card(card)
            if job is not None:
                out.append(job)
        return out

    def _parse_card(self, card: Any) -> Job | None:
        # ats_id lives on the wrapping <div value="..."> attribute. Some
        # sponsored cards omit the value attribute — fall back to a
        # rec_idx parsed out of the apply-button onclick.
        ats_id = (card.get("value") or "").strip()
        if not ats_id:
            for a in card.select("a[href*='rec_idx=']"):
                m = _REC_IDX_RE.search(a.get("href") or "")
                if m:
                    ats_id = m.group(1)
                    break
        if not ats_id:
            return None

        title_a = card.select_one("h2.job_tit a")
        if title_a is None:
            return None
        # Saramin renders the matched keyword wrapped in <b>...</b>;
        # ``get_text(strip=True)`` flattens that without losing chars,
        # but the ``title`` attribute is a cleaner copy without the
        # highlight markup so prefer it when present.
        title = (title_a.get("title") or title_a.get_text(strip=True)).strip()
        if not title:
            return None
        title = html.unescape(title)

        # Saramin's job detail URL is canonical — strip any tracking
        # querystring back to the bare rec_idx form so the row stays
        # stable across re-scrapes.
        url = JOB_URL_TEMPLATE.format(rec_idx=ats_id)

        company_a = card.select_one("strong.corp_name a")
        company = (
            company_a.get_text(strip=True) if company_a is not None
            else ""
        ).strip() or "Unknown"
        company = html.unescape(company)

        # ``<div class="job_condition">`` is a free-form sequence of
        # ``<span>`` elements: location anchors, career-level label,
        # education label, employment-type label. The position of each
        # within the list varies per card, so we classify each span by
        # content rather than positional index.
        condition = card.select_one("div.job_condition")
        location = None
        career_level = None
        education = None
        employment_type_label: str | None = None
        if condition is not None:
            location, career_level, education, employment_type_label = (
                _parse_condition(condition)
            )

        employment_type = _normalize_employment_type(employment_type_label)

        # ``등록일`` (registration date) is preferred over ``수정일``
        # (modified date) for ``posted_at`` semantics. The site renders
        # only one of these per card depending on the listing's age.
        sector_block = card.select_one("div.job_sector")
        posted_at = None
        modified_at = None
        if sector_block is not None:
            block_text = sector_block.get_text(" ", strip=True)
            posted_at = _parse_yymmdd(_POSTED_RE.search(block_text))
            modified_at = _parse_yymmdd(_MODIFIED_RE.search(block_text))

        # Deadline string ("~ 05/29(금)", "상시채용", "오늘마감", "채용시")
        # — keep verbatim. Useful for downstream "still-active" filters
        # without us trying to second-guess Korean calendar formatting.
        date_span = card.select_one("div.job_date span.date")
        deadline = date_span.get_text(strip=True) if date_span is not None else None

        # ``<div class="job_sector">`` also carries up to ~5 sector /
        # job-category anchors — capture verbatim text for raw.
        sector_tags: list[str] = []
        if sector_block is not None:
            sector_tags = [
                html.unescape(a.get_text(strip=True))
                for a in sector_block.select("a")
                if a.get_text(strip=True)
            ]

        raw: dict[str, Any] = {}
        if career_level:
            raw["career_level"] = career_level
        if education:
            raw["education"] = education
        if employment_type_label:
            raw["employment_type_label"] = employment_type_label
        if deadline:
            raw["deadline"] = deadline
        if modified_at is not None:
            raw["modified_at"] = modified_at.isoformat()
        if sector_tags:
            raw["sectors"] = sector_tags
        # Preserve the in-card apply URL when distinct (Saramin
        # sometimes routes the click through a tracking redirect).
        apply_href = (title_a.get("href") or "").strip()
        if apply_href:
            absolute_apply = urljoin(API_ROOT, apply_href)
            if absolute_apply != url:
                raw["search_url"] = absolute_apply

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.SARAMIN,
            ats_id=ats_id,
            location=location,
            country_iso="KR",
            language="ko",
            employment_type=employment_type,  # type: ignore[arg-type]
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _parse_condition(
    condition: Any,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Classify each ``<span>`` inside ``<div class="job_condition">``.

    The conditions block holds four orthogonal facets in arbitrary
    order: location (one or more region anchors), career level
    (``신입`` / ``경력`` / ``신입·경력`` / ``경력 5~20년``), education
    requirement (``학력무관`` / ``대졸 이상`` / ``석사↑`` …), and the
    employment-type label (``정규직`` / ``계약직`` / combos thereof).
    Classify by content rather than position because cards reorder
    them freely.
    """
    location: str | None = None
    career_level: str | None = None
    education: str | None = None
    employment_type_label: str | None = None
    for span in condition.find_all("span", recursive=False):
        text = span.get_text(" ", strip=True)
        if not text:
            continue
        text = html.unescape(text)
        # Location spans wrap one or more <a> tags pointing at the
        # area-list endpoint — they always show up first in practice
        # but we identify them structurally, not positionally.
        if span.find("a", href=True):
            location = re.sub(r"\s+", " ", text).strip() or None
            continue
        if any(keyword in text for keyword in _EMPLOYMENT_TYPE_MAP):
            employment_type_label = text
            continue
        if any(keyword in text for keyword in ("신입", "경력", "병역")):
            career_level = text
            continue
        # Anything else inside job_condition is the education facet
        # (학력무관 / 대졸 이상 / 석사↑ / 박사 / 고졸 / 초대졸 …).
        education = text
    return location, career_level, education, employment_type_label


def _normalize_employment_type(label: str | None) -> str | None:
    """Pick the canonical ``EmploymentType`` from a (possibly compound)
    Korean label like ``정규직·계약직``. Returns ``None`` when no token
    matches the known vocabulary."""
    if not label:
        return None
    matched: set[str] = set()
    for token, normalized in _EMPLOYMENT_TYPE_MAP.items():
        if token in label:
            matched.add(normalized)
    if not matched:
        return None
    for candidate in _EMPLOYMENT_TYPE_PRIORITY:
        if candidate in matched:
            return candidate
    return next(iter(matched))


def _parse_yymmdd(match: re.Match[str] | None) -> datetime | None:
    """Saramin renders dates as ``YY/MM/DD`` with 2-digit year. Assume
    20YY — the platform only existed past 2000 and the site itself
    treats years <50 as 20YY, ≥50 as 19YY. We never expect to see
    pre-2000 postings, so the simpler 20YY rule is safe."""
    if match is None:
        return None
    yy, mm, dd = match.groups()
    try:
        year = 2000 + int(yy)
        return datetime(year, int(mm), int(dd))
    except ValueError:
        return None
