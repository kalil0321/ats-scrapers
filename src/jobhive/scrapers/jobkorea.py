"""JobKorea (https://www.jobkorea.co.kr) — Korea's top job board.

JobKorea (잡코리아) is one of the two dominant direct-posting job
platforms in Korea — competing head-to-head with Saramin, with
hundreds of thousands of live postings across every industry.
Companies post directly through JobKorea's recruiting product.

The search page is a Next.js app whose server-rendered HTML embeds
the listing cards inline (each as a ``data-sentry-component="CardJob"``
block). We GET the integrated-search endpoint and pull listing cards
out of the response body via BeautifulSoup. Pagination is offset-style
through ``Page_No=N`` with ~25 cards per page; once the result set is
exhausted JobKorea silently falls back to the same 5-card sponsored
banner on every subsequent page (rather than returning a 4xx or an
empty page), so the scraper terminates when two consecutive pages
fail to introduce a new ``GI_No``.

Single-source scraper: ``company_slug`` is reused as the *stext*
(search term) so callers can scope a sweep to a vertical
(``"developer"``, ``"디자이너"``, ``"마케팅"`` …). Passing ``"any"`` /
empty falls back to the no-keyword landing — which JobKorea fills
with sponsored TOP banner cards (~20 per page, low signal). For
meaningful coverage, pick a keyword.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.jobkorea.co.kr"
SEARCH_PATH = "/Search/"
JOB_URL_TEMPLATE = "https://www.jobkorea.co.kr/Recruit/GI_Read/{gi_no}"
# JobKorea's UI renders 20-25 cards per page; this is informational
# (we don't actually pass it as a query param — the site doesn't
# honour a page-size param on this endpoint).
MAX_PAGES = 1000
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Empty stext on JobKorea renders the no-keyword landing with sponsored
# banner cards. Use the same sentinel set as wanted / saramin so
# single-source scrapers stay swappable across the codebase.
_EMPTY_SLUGS = {"", "any", "all", "jobkorea"}

# Korean employment-type tokens — same vocabulary as Saramin, since the
# two platforms compete for the same employer base and share label
# conventions. Combo strings ("정규직·계약직") resolve to the most
# permanent classification via ``_EMPLOYMENT_TYPE_PRIORITY``.
#
# - 정규직   : permanent / full-time
# - 계약직   : fixed-term contract
# - 인턴(직) : intern
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
_EMPLOYMENT_TYPE_PRIORITY = (
    "FULL_TIME", "INTERN", "CONTRACT", "PART_TIME", "TEMPORARY",
)

# JobKorea encodes the job id as a ``GI_No`` integer that also doubles
# as the ``GI_Read/{id}`` slug. The site sometimes wraps the link in
# ``?Oem_Code=...&listno=...`` tracking params; strip back to the bare
# id so the row stays stable across re-scrapes.
_GI_READ_RE = re.compile(r"/Recruit/GI_Read/(\d+)")


@ScraperRegistry.register(ATSType.JOBKOREA)
class JobKoreaScraper(BaseScraper):
    """JobKorea (jobkorea.co.kr) — Korea's top direct-posting platform.

    Single-source scraper: ``company_slug`` is used as the ``stext``
    (search term) query param so callers can scope sweeps to a vertical
    (e.g. ``JobKoreaScraper("개발자")`` for developer postings, or
    ``JobKoreaScraper("디자이너")`` for designer roles). Pass ``"any"``
    / ``""`` for an unscoped run (yields only the sponsored landing
    cards — coverage is intentionally narrow for that path).
    """

    ats = ATSType.JOBKOREA

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max(1, min(max_pages, MAX_PAGES))
        self._stext = (
            "" if company_slug.strip().lower() in _EMPTY_SLUGS
            else company_slug.strip()
        )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        # Detect end-of-results by tracking whether each page added a new
        # ``GI_No`` to the seen-set. Once two pages in a row fail to add
        # any new id we treat the sweep as exhausted — JobKorea silently
        # returns the same ~5 sponsored cards on out-of-range pages so a
        # pure HTTP-status / card-count check would loop forever.
        seen: set[str] = set()
        jobs: list[Job] = []
        consecutive_empty = 0
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page in range(1, self.max_pages + 1):
                page_html = await self._request_html(client, page)
                page_jobs = self._parse_page(page_html)
                added = 0
                for job in page_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    added += 1
                if added == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                else:
                    consecutive_empty = 0
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    def _build_url(self, page: int) -> str:
        stext = quote_plus(self._stext)
        return f"{API_ROOT}{SEARCH_PATH}?stext={stext}&Page_No={page}"

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> str:
        url = self._build_url(page)
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
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
                        f"JobKorea fetch failed for {url}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"JobKorea returned {response.status_code} for "
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
                f"JobKorea returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"JobKorea exhausted retries for {url}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_page(self, html_text: str) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ScraperError(
                "JobKorea scraper requires beautifulsoup4. Install with "
                "`pip install jobhive[scrapers]` or `pip install beautifulsoup4`."
            ) from exc

        soup = BeautifulSoup(html_text, "html.parser")
        # Cards are rendered by the ``CardJob`` Next.js component. The
        # outer ``<div data-sentry-component="CardJob">`` wraps every
        # listing — including sponsored / banner cards on the no-keyword
        # landing. We pick them by that attribute rather than a Tailwind
        # class chain (which is auto-generated and unstable).
        cards = soup.select('div[data-sentry-component="CardJob"]')
        out: list[Job] = []
        for card in cards:
            job = self._parse_card(card)
            if job is not None:
                out.append(job)
        return out

    def _parse_card(self, card: Any) -> Job | None:
        # ats_id lives inside ``/Recruit/GI_Read/{id}`` on any of the
        # card's anchors (title, company-name, logo). Pick the first
        # one we find — they all point at the same posting.
        ats_id: str | None = None
        for a in card.select("a[href*='/Recruit/GI_Read/']"):
            m = _GI_READ_RE.search(a.get("href") or "")
            if m:
                ats_id = m.group(1)
                break
        if not ats_id:
            return None

        # JobKorea wraps the actual title in the ``Title`` link
        # component; the visible span carries the human-readable string.
        title_link = card.select_one(
            'a[data-sentry-component="Title"] span'
        )
        title = (
            title_link.get_text(strip=True) if title_link is not None
            else ""
        )
        if not title:
            return None
        title = html.unescape(title)

        # The bare ``GI_Read/{id}`` form is the canonical posting URL —
        # strip the tracking querystring (``?Oem_Code=...&logpath=...``)
        # the site adds for analytics so re-scrapes stay stable.
        url = JOB_URL_TEMPLATE.format(gi_no=ats_id)

        # Company name lives in a sibling ``<a>`` immediately after the
        # title block — visually it's the smaller-typography line. We
        # pick the first non-Title anchor that points at GI_Read
        # (which on JobKorea also doubles as the company-link).
        company = _extract_company(card)

        # Each card has 1–3 ``GrayChip`` blocks beneath the title:
        # location, job-category breadcrumb, optional salary. Classify
        # by the embedded emoji icon class — the order is consistent
        # but a card may omit any subset, so we look up by icon rather
        # than positional index.
        location, sector, salary_label = _parse_chips(card)

        # The bottom row of the card shows experience requirement
        # (``경력무관`` / ``신입`` / ``경력4년↑`` …) in a small
        # ``text-typo-c1-13`` span. Employment-type may also surface
        # in the title prefix (``[정규직] ...``) or as a separate badge.
        experience_label = _extract_experience(card)

        employment_type_label, employment_type = _detect_employment_type(
            title
        )

        raw: dict[str, Any] = {}
        if sector:
            raw["sector"] = sector
        if salary_label:
            raw["salary_label"] = salary_label
        if experience_label:
            raw["experience"] = experience_label
        if employment_type_label:
            raw["employment_type_label"] = employment_type_label

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.JOBKOREA,
            ats_id=ats_id,
            location=location,
            country_iso="KR",
            language="ko",
            employment_type=employment_type,  # type: ignore[arg-type]
            description=sector or None,
            posted_at=None,  # JobKorea cards don't expose a 'posted at' field
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _extract_company(card: Any) -> str:
    """Pull the company name out of the card.

    The company-name anchor sits just below the title block. It points
    at the same ``GI_Read/{id}`` URL as the title link, but its inner
    ``<span class="truncate text-gray700 text-typo-b2-16">`` carries
    the company display name.
    """
    span = card.select_one("span.truncate.text-gray700.text-typo-b2-16")
    if span is not None:
        text = html.unescape(span.get_text(strip=True))
        if text:
            return text
    # Fallback: image alt text on the logo (``"{company} 로고"``) — used
    # when the company-name span is absent (rare; happens on a few
    # sponsored placements).
    logo = card.select_one("img[alt$=' 로고']")
    if logo is not None:
        alt = (logo.get("alt") or "").strip()
        if alt.endswith(" 로고"):
            return alt[:-3].strip() or "Unknown"
    return "Unknown"


def _parse_chips(
    card: Any,
) -> tuple[str | None, str | None, str | None]:
    """Classify each ``GrayChip`` block on the card.

    Three orthogonal chips may appear, in any subset:
      * ``place2`` icon → location (``서울 송파구 외 5``)
      * ``briefcase`` icon → job-category breadcrumb
      * ``money_bill`` icon → salary label (``월급 400~500만원``)

    Classify by the embedded emoji class so adding a new chip type
    on JobKorea's side doesn't silently break the mapping.
    """
    location: str | None = None
    sector: str | None = None
    salary_label: str | None = None
    for chip in card.select('div[data-sentry-component="GrayChip"]'):
        emoji = chip.select_one("span[class*='emoji--basicemoji-']")
        text_span = chip.select_one("span.truncate")
        if text_span is None:
            continue
        text = html.unescape(text_span.get_text(strip=True))
        if not text:
            continue
        emoji_classes = (emoji.get("class") if emoji is not None else []) or []
        emoji_class = " ".join(emoji_classes)
        if "place2" in emoji_class:
            location = text
        elif "briefcase" in emoji_class:
            sector = text
        elif "money_bill" in emoji_class:
            salary_label = text
    return location, sector, salary_label


def _extract_experience(card: Any) -> str | None:
    """Return the experience label (``경력무관`` / ``신입`` / ``경력4년↑``).

    The label lives in a small ``text-typo-c1-13`` span in the
    bottom-row of the card. JobKorea also reuses that class for the
    'today's hot post' badge in the top-right, so we restrict the
    search to spans whose text starts with an experience-related token.
    """
    for span in card.select("span.text-typo-c1-13"):
        text = html.unescape(span.get_text(strip=True))
        if not text:
            continue
        if any(
            text.startswith(prefix)
            for prefix in ("경력", "신입", "병역")
        ):
            return text
    return None


def _detect_employment_type(
    title: str,
) -> tuple[str | None, str | None]:
    """Look for an employment-type token in the listing title.

    JobKorea cards don't expose a structured employment-type field on
    the listing card the way Saramin does; employers conventionally
    prefix the title with the type in square brackets (``[정규직]``,
    ``[계약직]``, ``[인턴]``) when it's notable. We surface both the
    raw token and the normalized enum value.
    """
    if not title:
        return None, None
    matched: set[str] = set()
    raw_tokens: list[str] = []
    for token, normalized in _EMPLOYMENT_TYPE_MAP.items():
        if token in title:
            matched.add(normalized)
            raw_tokens.append(token)
    if not matched:
        return None, None
    for candidate in _EMPLOYMENT_TYPE_PRIORITY:
        if candidate in matched:
            return raw_tokens[0], candidate
    return raw_tokens[0], next(iter(matched))
