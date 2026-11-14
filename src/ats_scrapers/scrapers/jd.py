"""JD.com (京东) careers scraper.

JD.com publishes the openings of the whole group (JD Logistics, JD Retail,
JD Health, ...) on one careers site, ``https://zhaopin.jd.com``. Its list
page loads them from two public endpoints that need no login::

    POST https://zhaopin.jd.com/web/job/job_count   -> the total, a bare integer
    POST https://zhaopin.jd.com/web/job/job_list    -> a JSON array of openings

Both take the page's filters (left empty here) and the list takes
``pageIndex`` / ``pageSize``. ``pageSize`` is capped at 100. The page sends
them as a form body; the same values are accepted as query parameters, which
is what this scraper sends. A GET is redirected to the login page.

Each opening carries its whole text (``workContent`` and ``qualification``),
so nothing else is read.

There is no page per opening. On the site an opening is a row of the list
that unfolds in place, and applying requires an account. ``Job.url`` is
therefore the list page, with the opening's id as a fragment so that every
job keeps an address of its own; the fragment is not something the site
reads.

The list moves while it is read (openings are published all day), so a row
can show on two pages while another is skipped. The pages are read again, up
to ``MAX_PASSES`` times, until every announced opening has been seen.

The rows also name the staff who opened or own the requisition. Nothing
about a person is read or stored.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

LIST_URL = "https://zhaopin.jd.com/web/job/job_list"
COUNT_URL = "https://zhaopin.jd.com/web/job/job_count"
BOARD_URL = "https://zhaopin.jd.com/web/job/job_info_list/3"
COMPANY = "JD.com"
# The list answers at most this many openings per page.
PAGE_SIZE = 100
# Far above what the group publishes (about 1,700 openings in November
# 2026): a total that needs more pages is not believed.
MAX_PAGES = 200
PAGE_CONCURRENCY = 2
# How many times the pages are read to collect the openings the moving list
# skipped, and the share of the announced total below which the list is
# refused.
MAX_PASSES = 3
MIN_COVERAGE = 0.98
MAX_DESCRIPTION_CHARS = 25_000

# The page's filters, left empty: every business unit, job family and place.
_NO_FILTER = {
    "workCityJson": "[]",
    "jobTypeJson": "[]",
    "jobSearch": "",
    "depTypeJson": "[]",
}
# ``workCityCode`` is the two-digit code of a province-level division of
# China (GB/T 2260): 11 Beijing ... 65 Xinjiang.
_MAINLAND_CODE_RE = re.compile(r"(?:1[1-5]|2[1-3]|3[1-7]|4[1-6]|5[0-4]|6[1-5])")
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.JD)
class JDScraper(BaseScraper):
    """Scrape the openings of the JD.com group.

    ``company_slug`` is not used: the careers site is one list for the whole
    group. The business unit of an opening is in ``Job.department``.
    """

    ats = ATSType.JD

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://zhaopin.jd.com",
        "Referer": BOARD_URL,
    }

    async def afetch(self) -> list[Job]:
        found: dict[str, dict[str, Any]] = {}
        total = 0
        async with self.make_fetcher() as fetch:
            semaphore = asyncio.Semaphore(PAGE_CONCURRENCY)

            async def read(page: int) -> list[dict[str, Any]]:
                async with semaphore:
                    return await self._page(fetch, page)

            for _ in range(MAX_PASSES):
                total = await self._total(fetch)
                pages = math.ceil(total / PAGE_SIZE)
                rows = await asyncio.gather(
                    *(read(page) for page in range(1, pages + 1))
                )
                for page_rows in rows:
                    for row in page_rows:
                        found.setdefault(_opening_id(row), row)
                if len(found) >= total:
                    break

        # ``total`` is the one the last pass announced.
        if len(found) < math.ceil(total * MIN_COVERAGE):
            raise ScraperError(
                f"JD.com announces {total} openings and listed {len(found)} "
                f"in {MAX_PASSES} passes"
            )
        fetched_at = datetime.now(UTC)
        return [self._parse_job(row, fetched_at) for row in found.values()]

    async def _total(self, fetch: Fetcher) -> int:
        response = await fetch.request("POST", COUNT_URL, params=_NO_FILTER)
        text = response.text.strip()
        if not text.isdigit():
            raise ScraperError(f"JD.com gave no total: {text[:80]!r}")
        total = int(text)
        if total > MAX_PAGES * PAGE_SIZE:
            raise ScraperError(
                f"JD.com announces {total} openings, more than the "
                f"{MAX_PAGES * PAGE_SIZE} this scraper believes"
            )
        return total

    async def _page(self, fetch: Fetcher, page: int) -> list[dict[str, Any]]:
        response = await fetch.request(
            "POST",
            LIST_URL,
            params={**_NO_FILTER, "pageIndex": page, "pageSize": PAGE_SIZE},
        )
        rows = response.json()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ScraperError(f"JD.com page {page} was not a list of openings")
        return rows

    def _parse_job(self, row: dict[str, Any], fetched_at: datetime) -> Job:
        opening_id = _opening_id(row)
        title = _text(row.get("positionNameOpen")) or _text(row.get("positionName"))
        if title is None:
            raise ScraperError(f"JD.com opening {opening_id} has no title")
        code = _text(row.get("workCityCode"))
        description = _description(row)
        raw = {
            key: value
            for key, source in (
                ("listing_id", "id"),
                ("position_id", "positionId"),
                ("position_code", "positionCode"),
                ("job_type_code", "jobTypeCode"),
                ("work_city_code", "workCityCode"),
            )
            if (value := row.get(source)) not in (None, "")
        }
        return Job(
            url=f"{BOARD_URL}#requirement-{opening_id}",
            title=title,
            company=COMPANY,
            ats_type=ATSType.JD,
            ats_id=opening_id,
            location=_text(row.get("workCity")),
            country_iso="CN" if code and _MAINLAND_CODE_RE.fullmatch(code) else None,
            department=_text(row.get("positionDeptName")),
            team=_text(row.get("jobType")),
            requisition_id=_text(row.get("reqNumber")),
            description=(
                description[:MAX_DESCRIPTION_CHARS]
                if description and self.include_descriptions
                else None
            ),
            posted_at=_parse_epoch_ms(row.get("publishTime")),
            fetched_at=fetched_at,
            language="zh",
            raw=raw or None,
        )


def _opening_id(row: dict[str, Any]) -> str:
    """The id of the requisition an opening stands for."""
    value = row.get("requirementId")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScraperError("JD.com listed an opening without its requirementId")
    return str(value)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _block(value: object) -> str | None:
    """A text field with its line breaks kept and any markup removed."""
    if not isinstance(value, str):
        return None
    lines = [" ".join(_TAG_RE.sub(" ", line).split()) for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip()) or None


def _description(row: dict[str, Any]) -> str | None:
    """The two parts of an opening under the headings the site gives them."""
    parts = [
        f"{heading}\n{text}"
        for heading, key in (("岗位描述：", "workContent"), ("任职要求：", "qualification"))
        if (text := _block(row.get(key)))
    ]
    return "\n\n".join(parts) or None


def _parse_epoch_ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None
