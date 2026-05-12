"""JD.com (京东) careers scraper.

JD.com is one of China's top 3 e-commerce/retail giants, comprising
JD Retail (京东零售), JD Industrials (京东工业), JD Logistics (京东物流),
JD Health (京东健康), and other subsidiaries — all visible in the
``positionDeptName`` field of each posting.

The careers backend at https://zhaopin.jd.com/web/job/job_list is a
public, no-auth POST endpoint that returns a **flat JSON array** of job
objects (no wrapper, no metadata, no total in the body). The endpoint
expects a **form-encoded** body — despite what one might guess from a
JSON-style URL — with ``pageIndex``/``pageSize`` parameters. A sibling
``job_count`` endpoint returns the overall total as a bare integer, used
here for an early upper bound on pagination.

The companion ``job_detail`` page redirects through JD's passport
SSO for any logged-out visitor — we keep ``url`` pointing at the
canonical job-detail URL anyway since downstream consumers can still
follow it (the redirect lands back at zhaopin.jd.com after auth).

The listing is Chinese-only (``zh``). All Chinese category/department
labels are passed through verbatim — translation is downstream LLM
enrichment's job.
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

LIST_URL = "https://zhaopin.jd.com/web/job/job_list"
COUNT_URL = "https://zhaopin.jd.com/web/job/job_count"
DEFAULT_PAGE_SIZE = 100  # Server hard-caps at 100; higher silently truncates.
DEFAULT_MAX_PAGES = 500
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://zhaopin.jd.com",
    "Referer": "https://zhaopin.jd.com/web/job/job_info_list/3",
}

# Empty-filter form body — matches what the public web UI sends when no
# search params are selected.
_EMPTY_FILTERS = {
    "workCityJson": "[]",
    "jobTypeJson": "[]",
    "jobSearch": "",
    "depTypeJson": "[]",
}


@ScraperRegistry.register(ATSType.JD)
class JDScraper(BaseScraper):
    """JD.com careers scraper.

    ``company_slug`` is informational only — JD's single careers surface
    exposes every subsidiary in one feed. ``positionDeptName`` records
    which JD subsidiary (JD Retail / Industrials / Logistics / Health)
    a given role belongs to.
    """

    ats = ATSType.JD

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.page_size = min(max(page_size, 1), 100)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Page 1 always — sets the deduplication baseline and gives
            # us an early-exit signal if the feed is smaller than one
            # page.
            first_posts = await self._fetch_page(client, sem, page=1)
            jobs = self._parse_posts(first_posts)
            if len(first_posts) < self.page_size:
                return jobs

            # The sibling ``job_count`` endpoint returns the total as a
            # bare integer; if it succeeds we use it to bound pagination
            # tightly. If it fails or returns nonsense, fall back to
            # ``max_pages`` and rely on the short-page break inside
            # ``one()``.
            total = await self._fetch_count(client, sem)
            if total > 0:
                page_count = min(
                    (total + self.page_size - 1) // self.page_size,
                    self.max_pages,
                )
            else:
                page_count = self.max_pages

            seen: set[str | None] = {j.ats_id for j in jobs}
            lock = asyncio.Lock()
            stop = asyncio.Event()

            async def one(page: int) -> None:
                if stop.is_set():
                    return
                posts = await self._fetch_page(client, sem, page=page)
                if not posts:
                    # Empty page means we've walked off the end — signal
                    # remaining workers to stop fetching.
                    stop.set()
                    return
                page_jobs = self._parse_posts(posts)
                async with lock:
                    for job in page_jobs:
                        if job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)
                if len(posts) < self.page_size:
                    stop.set()

            await asyncio.gather(*(one(p) for p in range(2, page_count + 1)))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> list[dict[str, Any]]:
        data = {
            "pageIndex": str(page),
            "pageSize": str(self.page_size),
            **_EMPTY_FILTERS,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.post(
                        LIST_URL, data=data, headers=HEADERS
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"JD.com fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"JD.com returned non-JSON at page={page}: {exc}"
                    ) from exc
                if not isinstance(payload, list):
                    raise ScraperError(
                        f"JD.com returned unexpected payload shape at "
                        f"page={page}: {type(payload).__name__}"
                    )
                return payload
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"JD.com returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"JD.com returned {response.status_code} at page={page}"
            )
        raise ScraperError(f"JD.com exhausted retries at page={page}: {last_exc}")

    async def _fetch_count(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore
    ) -> int:
        """Probe the sibling ``job_count`` endpoint for the total.

        Returns 0 on any failure — callers fall back to a page-cap.
        Failures are intentionally silent: a missing count is not fatal,
        it just means we walk pages until they short-return.
        """
        async with sem:
            try:
                response = await client.post(
                    COUNT_URL, data=_EMPTY_FILTERS, headers=HEADERS
                )
            except httpx.HTTPError:
                return 0
        if response.status_code != 200:
            return 0
        try:
            return int(response.text.strip())
        except (ValueError, AttributeError):
            return 0

    def _parse_posts(self, posts: list[dict[str, Any]]) -> list[Job]:
        return [job for post in posts if (job := self._parse_post(post)) is not None]

    def _parse_post(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("positionId") or item.get("id") or "")
        # Prefer ``positionNameOpen`` when set (the publicly-facing
        # marketing title) — it's frequently identical to ``positionName``
        # but occasionally more specific (e.g. "服务器采销岗" vs "采销岗").
        title = _first_nonempty(item.get("positionNameOpen"), item.get("positionName"))
        if not ats_id or not title:
            return None

        description = _compose_description(
            item.get("workContent"),
            item.get("qualification"),
        )

        # ``publishTime`` is epoch milliseconds; ``formatPublishTime`` is
        # the same value already rendered as ``YYYY-MM-DD`` in CST. We
        # prefer the numeric form for precision.
        posted_at = _parse_epoch_ms(item.get("publishTime")) or _parse_date(
            item.get("formatPublishTime")
        )

        raw: dict[str, Any] = {}
        for source, dest in (
            ("positionCode", "position_code"),
            ("workCityCode", "work_city_code"),
            ("jobTypeCode", "job_type_code"),
            ("requirementId", "requirement_id"),
            ("isHot", "is_hot"),
        ):
            value = item.get(source)
            if value not in (None, ""):
                raw[dest] = value

        req_number = item.get("reqNumber")
        requisition_id = (
            req_number.strip()
            if isinstance(req_number, str) and req_number.strip()
            else None
        )

        return Job(
            url=f"https://zhaopin.jd.com/web/job/job_detail?jobId={ats_id}",
            title=title,
            company="JD.com",
            ats_type=ATSType.JD,
            ats_id=ats_id,
            location=_first_nonempty(item.get("workCity")),
            country_iso="CN",
            department=_first_nonempty(item.get("jobType")),
            team=_first_nonempty(item.get("positionDeptName")),
            requisition_id=requisition_id,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language="zh",
            raw=raw or None,
        )


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags from a description fragment.

    JD.com listings are almost always plain text with newlines, but the
    odd posting smuggles in ``<br/>`` or ``<p>`` tags from the WYSIWYG
    editor. Cheap regex strip is enough — no nested-tag edge cases here.
    """
    return _HTML_TAG_RE.sub("", text)


def _compose_description(*sources: object) -> str | None:
    """Concatenate description-like fields, strip HTML, cap at 10kB."""
    parts: list[str] = []
    for source in sources:
        if isinstance(source, str) and source.strip():
            parts.append(_strip_html(source).strip())
    if not parts:
        return None
    text = "\n\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:10_000] or None


def _first_nonempty(*values: object) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_epoch_ms(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (ValueError, OSError, OverflowError):
        return None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None
