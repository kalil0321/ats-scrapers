"""MyNavi Tenshoku (``https://tenshoku.mynavi.jp``) — Japanese mid-career job
board scraper.

MyNavi Tenshoku (転職) is one of Japan's top-3 mid-career job platforms
(~54k live postings). Companies post directly through MyNavi — not an
aggregator of LinkedIn / Indeed feeds.

There is no public JSON API: the listing pages are server-side rendered
HTML and the in-page SPA layer only hydrates UI chrome. We parse the
HTML directly via tight regexes against MyNavi's stable
``cassetteRecruit`` card markup.

Listing URL pattern::

    https://tenshoku.mynavi.jp/list/        # page 1
    https://tenshoku.mynavi.jp/list/pg{N}/  # subsequent pages (2..1083)

Each page server-renders 50 cards (40 ``cassetteRecruit`` + 10
``cassetteRecruitRecommend``). The two card variants share the same
inner ``tableCondition`` schema; we parse them with a single regex pass.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the wanted / eures / bundesagentur pattern). Output rows
carry the publishing employer's name verbatim from ``cassetteRecruit__name``.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Literal

    EmploymentTypeLiteral = Literal[
        "FULL_TIME", "PART_TIME", "CONTRACT", "INTERN", "TEMPORARY"
    ]

BASE_URL = "https://tenshoku.mynavi.jp"
LISTING_FIRST = "/list/"
LISTING_PAGE = "/list/pg{n}/"

# MyNavi caps useful pagination at ~1083 pages today (54k jobs / 50 per
# page) but the listing exposes more sparse pages further out for
# bookmarked filters. ``MAX_PAGES`` is a hard ceiling so a server-side
# regression returning duplicate pages doesn't spin us forever — set
# generously above the live total.
MAX_PAGES = 1500
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Empty pages (out-of-range) return HTTP 200 with a "Page not found"
# template that has zero card markers. Detect via the absence of any
# ``cassetteRecruit`` block rather than a status code.
_NOT_FOUND_MARKER = "お探しのページは見つかりませんでした"

# Map MyNavi employment-status labels → canonical Job.employment_type.
# ``正社員`` (seishain) = regular full employment; ``契約社員`` /
# ``派遣社員`` are fixed-term / dispatch workers (both CONTRACT under
# our schema). ``アルバイト・パート`` is the casual hourly bucket.
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentTypeLiteral] = {
    "正社員": "FULL_TIME",
    "契約社員": "CONTRACT",
    "派遣社員": "CONTRACT",
    "業務委託": "CONTRACT",
    "アルバイト": "PART_TIME",
    "パート": "PART_TIME",
    "アルバイト・パート": "PART_TIME",
    "インターン": "INTERN",
    "インターンシップ": "INTERN",
    "新卒": "FULL_TIME",
}

# Each card is one of these two variants — same inner structure.
_CARD_RE = re.compile(
    r'<div class="cassetteRecruit(?:Recommend)?(?:__content)? '
    r'js__link--post"[^>]*>(?P<body>.*?)'
    r'(?=<div class="cassetteRecruit(?:Recommend)?(?:__content)? '
    r'js__link--post"|</body>)',
    re.DOTALL,
)
# Stable per-posting key (numeric). Distinct from the company-scoped
# ``jobinfo-{company}-{?}-{seq}`` URL slug — every card has exactly one
# ``data-job-key``; same company across multiple postings gets a
# different key per posting.
_JOB_KEY_RE = re.compile(r'data-job-key="(?P<key>\d+)"')
# Public detail URL slug. Always shaped ``jobinfo-{N}-{N}-{N}``.
_JOBINFO_PATH_RE = re.compile(
    r'href="(?P<href>//tenshoku\.mynavi\.jp/(?P<slug>jobinfo-\d+-\d+-\d+)[^"]*)"'
)
_COMPANY_RE = re.compile(
    r'class="cassetteRecruit(?:Recommend)?__name">(?P<name>[^<]+)</'
)
_TITLE_RE = re.compile(
    r'class="cassetteRecruit(?:Recommend)?__copy boxAdjust">\s*'
    r'<a[^>]*href="[^"]*"[^>]*>(?P<title>[^<]+)</a>'
)
_EMPLOYMENT_RE = re.compile(
    r'<span class="labelEmploymentStatus">(?P<label>[^<]+)</span>'
)
# Inner field table — same shape on both card variants.
_TABLE_ROW_RE = re.compile(
    r'<th class="tableCondition__head">(?P<head>[^<]+)</th>\s*'
    r'<td class="tableCondition__body">(?P<body>[^<]*)</td>'
)
# 情報更新日 (info update date) — single-row block, YYYY/MM/DD.
_UPDATE_DATE_RE = re.compile(
    r'class="cassetteRecruit(?:Recommend)?__updateDate">[^<]*'
    r'<span>\s*(?P<date>\d{4}/\d{1,2}/\d{1,2})\s*</span>'
)
# Feature tags (赤いチップ的なやつ) — multiple per card.
_FEATURE_TAG_RE = re.compile(
    r'class="labelCondition">(?P<tag>[^<]+)</span>'
)
# Salary range in 万円 (= 10,000 JPY) units. Accept both the wide and
# narrow tilde MyNavi mixes interchangeably.
_SALARY_RANGE_RE = re.compile(
    r'(?P<min>\d{2,5})\s*万円\s*[～~〜]\s*(?P<max>\d{2,5})\s*万円'
)

# MyNavi card field heads we care about. ``勤務地`` is the location
# string (free text — Japanese, prose); ``給与`` is the monthly /
# hourly salary description and we typically prefer the structured
# ``初年度年収`` (first-year annual income) range when both are
# present.
_FIELD_LOCATION = "勤務地"
_FIELD_SALARY_FREE = "給与"
_FIELD_SALARY_FIRST_YEAR = "初年度年収"
_FIELD_JOB_CONTENT = "仕事内容"
_FIELD_TARGET = "対象となる方"


@ScraperRegistry.register(ATSType.MYNAVI)
class MyNaviScraper(BaseScraper):
    """MyNavi Tenshoku (tenshoku.mynavi.jp) — Japan mid-career job board.

    Single-source scraper: ``company_slug`` is ignored — pass anything
    (``"any"``, ``""``). The scraper walks the entire site pagination
    until an empty page is returned.

    To cap the walk for smoke tests, pass ``max_pages``::

        MyNaviScraper("any", max_pages=2).fetch()
    """

    ats: ClassVar[ATSType] = ATSType.MYNAVI

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            for page in range(1, self.max_pages + 1):
                path = LISTING_FIRST if page == 1 else LISTING_PAGE.format(n=page)
                html_text = await self._request_html(client, sem, path)
                if _NOT_FOUND_MARKER in html_text:
                    break
                page_jobs = list(self._parse_listing(html_text))
                if not page_jobs:
                    # Genuine end-of-listing — MyNavi serves the
                    # "page not found" template on out-of-range pages,
                    # but defensively we also bail on an empty card
                    # pass (e.g. heading-only response).
                    break
                added = 0
                for job in page_jobs:
                    if job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    added += 1
                # If a page contained only duplicates we've already
                # seen, the pagination cursor has wrapped — stop.
                if added == 0:
                    break
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _request_html(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        path: str,
    ) -> str:
        url = f"{BASE_URL}{path}"
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
                            "Accept": "text/html",
                            "Accept-Language": "ja-JP,ja;q=0.9",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"MyNavi fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"MyNavi returned {response.status_code} for "
                        f"{url} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code == 404:
                # Bookmarked filter that no longer matches anything.
                # Treat as empty rather than fatal.
                return ""
            raise ScraperError(
                f"MyNavi returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"MyNavi exhausted retries for {url}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_listing(self, html_text: str):
        """Yield ``Job`` instances parsed from a single listing page.

        Each card is delimited by an opening
        ``cassetteRecruit[Recommend]__content js__link--post`` div.
        The closing tag is unreliable (MyNavi mixes container divs with
        broken/indented HTML), so we slice on the next card-opening
        instead and let the inner regexes work on the (slightly
        oversized) chunk.
        """
        for match in _CARD_RE.finditer(html_text):
            body = match.group("body")
            job = self._parse_card(body)
            if job is not None:
                yield job

    def _parse_card(self, body: str) -> Job | None:
        key_match = _JOB_KEY_RE.search(body)
        path_match = _JOBINFO_PATH_RE.search(body)
        title_match = _TITLE_RE.search(body)
        company_match = _COMPANY_RE.search(body)
        if not (path_match and title_match and company_match):
            return None

        slug = path_match.group("slug")
        # ``data-job-key`` is the most stable per-posting identifier:
        # unique across the whole listing and survives URL slug rewrites
        # (which MyNavi does occasionally as the ``-{seq}`` counter ticks).
        # Fall back to the URL slug when (rare) the button block is
        # missing — defends against incomplete card renders.
        ats_id = key_match.group("key") if key_match else slug

        title = html.unescape(title_match.group("title").strip())
        company = html.unescape(company_match.group("name").strip())
        if not title or not company:
            return None

        # Build the public detail URL. ``href`` is protocol-relative
        # (``//tenshoku.mynavi.jp/...``) — pin https.
        href = path_match.group("href")
        url = f"https:{href}" if href.startswith("//") else f"{BASE_URL}{href}"

        fields: dict[str, str] = {}
        for row in _TABLE_ROW_RE.finditer(body):
            head = html.unescape(row.group("head").strip())
            value = html.unescape(row.group("body").strip())
            if head and value:
                fields[head] = value

        location = fields.get(_FIELD_LOCATION) or None
        # Prefer the structured ``初年度年収`` range over the free-text
        # ``給与`` field — it's a clean ``{min}万円～{max}万円`` pattern.
        salary_summary = (
            fields.get(_FIELD_SALARY_FIRST_YEAR)
            or fields.get(_FIELD_SALARY_FREE)
            or None
        )
        salary_min, salary_max = _parse_salary_yen(salary_summary)

        # Card description: concatenate ``仕事内容`` and ``対象となる方``
        # if both present — these are the two free-text teasers MyNavi
        # surfaces on the listing card. Keep it small (~1kB) per card;
        # the full description is on the detail page (out of scope for
        # this scraper).
        desc_parts = [fields.get(_FIELD_JOB_CONTENT), fields.get(_FIELD_TARGET)]
        description = "\n\n".join(p for p in desc_parts if p) or None

        emp_match = _EMPLOYMENT_RE.search(body)
        employment_label: str | None = None
        employment_type: EmploymentTypeLiteral | None = None
        if emp_match:
            employment_label = html.unescape(emp_match.group("label").strip())
            employment_type = _EMPLOYMENT_TYPE_MAP.get(employment_label)

        # ``情報更新日`` is the last-edit date, not the publication date:
        # a refreshed old posting reports a recent update date, so mapping
        # it to ``posted_at`` would make stale jobs look newly posted.
        # Leave ``posted_at`` None and preserve the raw update date instead.
        upd_match = _UPDATE_DATE_RE.search(body)
        update_date = upd_match.group("date") if upd_match else None

        feature_tags = [
            html.unescape(t.group("tag").strip())
            for t in _FEATURE_TAG_RE.finditer(body)
        ]
        feature_tags = [t for t in feature_tags if t]

        raw: dict[str, object] = {"jobinfo_slug": slug}
        if update_date:
            raw["update_date"] = update_date
        if feature_tags:
            raw["feature_tags"] = feature_tags
        if employment_label and employment_label not in _EMPLOYMENT_TYPE_MAP:
            raw["employment_label_raw"] = employment_label
        # Preserve the free-text salary string even when we used the
        # structured first-year range, since the prose carries monthly /
        # bonus context the range strips.
        if (
            fields.get(_FIELD_SALARY_FREE)
            and fields.get(_FIELD_SALARY_FREE) != salary_summary
        ):
            raw["salary_free_text"] = fields[_FIELD_SALARY_FREE]

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.MYNAVI,
            ats_id=ats_id,
            location=location,
            country_iso="JP",
            language="ja",
            description=description,
            employment_type=employment_type,
            commitment=employment_label,
            salary_currency="JPY" if (salary_min or salary_max) else None,
            salary_period="YEAR" if (salary_min or salary_max) else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_at=None,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


# --- helpers ----------------------------------------------------------------


def _parse_salary_yen(text: str | None) -> tuple[float | None, float | None]:
    """Extract ``(min, max)`` in JPY from a ``{N}万円～{M}万円`` string.

    MyNavi's first-year-income field uses 万 (= 10,000) consistently —
    a ``400万円`` range bound is 4,000,000 JPY. Returns ``(None, None)``
    when no range is found (e.g. a single-bound ``◇月給25万円〜34万円``
    monthly figure from the free-text ``給与`` field — those need
    different unit logic and are out of scope here).
    """
    if not text:
        return None, None
    match = _SALARY_RANGE_RE.search(text)
    if not match:
        return None, None
    try:
        lo = float(match.group("min")) * 10_000
        hi = float(match.group("max")) * 10_000
    except ValueError:
        return None, None
    if lo > hi:
        return None, None
    return lo, hi
