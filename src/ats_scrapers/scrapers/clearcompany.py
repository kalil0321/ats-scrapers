"""ClearCompany career sites.

A ClearCompany customer publishes its openings on a hosted career site:

    https://apply.clearcompany.com/{site}

(the older ``{site}.hrmdirect.com/employment/`` addresses redirect there).
The page is a shell around ClearCompany's job widget. It names the site id
the widget reads:

    <script src=".../career-site.js?hosted=true"
            data-site-id="41faeaee-3ddd-83bd-84dc-37aa557a1e29"></script>

and the widget lists the openings from:

    GET https://careers-api.clearcompany.com/v1/{siteId}?pageIndex=0&pageSize=100

    {"results": [{"id": "dc63d201-…", "userDefinedId": "Q8A86289",
                  "positionTitle": "Food Service Worker",
                  "description": "<p>…</p>",
                  "postedDate": "2026-11-12T00:00:00",
                  "departmentName": "…", "jobFunctionName": null,
                  "location": "MOORESVILLE NC",
                  "locations": [{"city": "MOORESVILLE", "subdivision": "NC",
                                 "country": "US", "isRemote": false, …}],
                  "brandName": "AVI Foodsystems",
                  "applyLink": "https://{tenant}.clearcompany.com/careers/jobs/{id}/apply",
                  "salaryRangeLow": "0", "salaryRangeHigh": "0",
                  "salaryType": null}],
     "currentPageIndex": 0, "totalCount": 1114, "currentPageCount": 100}

Pages are numbered from 0 and a page past the end has no result. The list
carries the description, so no posting needs a request of its own.

The walk reads on to an empty page, or to a short page once the rows read
reach ``totalCount``. It raises when a page fails, when the rows it could
read fall short of the rows listed, or when the postings it ends with fall
short of ``totalCount``; it never returns a partial list.

``company_slug`` is the site name or the career site URL. A posting's URL
is the address the widget gives it on the career site:

    https://apply.clearcompany.com/{site}?jobId={id}

The same posting can be listed on several career sites of one customer (a
group site and a brand site), so ``ats_id`` is the posting's own id, which
is the same on each of them.
"""

from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

SITE_HOST = "apply.clearcompany.com"
API_ROOT = "https://careers-api.clearcompany.com/v1"
PAGE_SIZE = 100
# 500 pages is 50,000 postings: past this the walk raises instead of
# going on.
MAX_PAGES = 500
# At least this share of the rows listed must parse into a job, and at
# least this share of the announced total must be reached.
MIN_COVERAGE = 0.9
MAX_DESCRIPTION_CHARS = 25_000

_SITE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_SITE_ID_RE = re.compile(
    r"\bdata-site-id\s*=\s*[\"']"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\"']",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_APPLY_HOST_RE = re.compile(
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.clearcompany\.com", re.IGNORECASE
)
_WHITESPACE_RE = re.compile(r"\s+")

_SALARY_PERIODS = {"per hour": "HOUR", "per year": "YEAR"}


@ScraperRegistry.register(ATSType.CLEARCOMPANY)
class ClearCompanyScraper(BaseScraper):
    """All postings of one ClearCompany career site."""

    ats = ATSType.CLEARCOMPANY
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        try:
            self.site = _parse_slug(company_slug)
        except ValueError as exc:
            raise ScraperError(str(exc)) from exc
        self.company_name = (company_name or "").strip() or None
        self.site_url = f"https://{SITE_HOST}/{self.site}"

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            site_id = await self._site_id(fetch)
            return await self._walk(fetch, site_id)

    async def _site_id(self, fetch: Fetcher) -> str:
        page = await fetch.get_text(
            self.site_url, headers={"Accept": "text/html,application/xhtml+xml"}
        )
        match = _SITE_ID_RE.search(page)
        if match is None:
            raise ScraperError(
                f"ClearCompany ({self.site}): the career site names no site id"
            )
        return match.group(1).lower()

    async def _walk(self, fetch: Fetcher, site_id: str) -> list[Job]:
        url = f"{API_ROOT}/{site_id}"
        jobs: dict[str, Job] = {}
        rows_seen = 0
        rows_read = 0
        total = 0
        for page in range(MAX_PAGES):
            payload = await fetch.get_json(
                url,
                params={"pageIndex": page, "pageSize": PAGE_SIZE},
                headers={"Accept": "application/json"},
            )
            rows, total = self._check_payload(payload, page)
            for row in rows:
                job = self._parse_row(row)
                if job is not None and job.ats_id:
                    rows_read += 1
                    jobs.setdefault(job.ats_id, job)
            rows_seen += len(rows)
            if not rows:
                break
            if len(rows) < PAGE_SIZE and rows_seen >= total:
                break
        else:
            raise ScraperError(
                f"ClearCompany ({self.site}) still lists postings after "
                f"{MAX_PAGES} pages; refusing to return a partial list"
            )
        if rows_read < math.ceil(rows_seen * MIN_COVERAGE):
            raise ScraperError(
                f"ClearCompany ({self.site}) could read only {rows_read} of "
                f"the {rows_seen} postings listed"
            )
        # ``total`` is the one the last page announced.
        if len(jobs) < math.ceil(total * MIN_COVERAGE):
            raise ScraperError(
                f"ClearCompany ({self.site}) announces {total} postings and "
                f"listed {len(jobs)}; refusing to return a partial list"
            )
        return list(jobs.values())

    def _check_payload(
        self, payload: object, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Validate one page. Returns its rows and the announced total."""
        if not isinstance(payload, dict):
            raise ScraperError(
                f"ClearCompany ({self.site}) page {page}: expected a JSON object"
            )
        rows = payload.get("results")
        total = _count(payload.get("totalCount"))
        if not isinstance(rows, list) or total is None:
            raise ScraperError(
                f"ClearCompany ({self.site}) page {page}: response has no "
                f"posting list or total"
            )
        if any(not isinstance(row, dict) for row in rows):
            raise ScraperError(
                f"ClearCompany ({self.site}) page {page}: a posting is not a "
                f"JSON object"
            )
        current = _count(payload.get("currentPageIndex"))
        if current is not None and current != page:
            raise ScraperError(
                f"ClearCompany ({self.site}) answered page {current} when "
                f"asked for page {page}"
            )
        return rows, total

    def _parse_row(self, row: dict[str, Any]) -> Job | None:
        job_id = _text(row.get("id"))
        title = _text(row.get("positionTitle"))
        if not job_id or not _UUID_RE.fullmatch(job_id) or not title:
            return None
        job_id = job_id.lower()

        listed = row.get("locations")
        places = (
            [place for place in listed if isinstance(place, dict)]
            if isinstance(listed, list)
            else []
        )
        named = []
        for place in places:
            label = ", ".join(
                part
                for part in (
                    _text(place.get("city")),
                    _text(place.get("subdivision")),
                    _text(place.get("country")),
                )
                if part
            )
            if label and label not in named:
                named.append(label)
        location = "; ".join(named) or _text(row.get("location"))

        remote_flags = [place.get("isRemote") for place in places]
        is_remote: bool | None = None
        if any(flag is True for flag in remote_flags):
            is_remote = True
        elif remote_flags and all(flag is False for flag in remote_flags):
            is_remote = False

        countries = {
            country.upper()
            for country in (_text(place.get("country")) for place in places)
            if country
        }
        country_iso = (
            next(iter(countries))
            if len(countries) == 1 and len(next(iter(countries))) == 2
            else None
        )

        salary_min = _amount(row.get("salaryRangeLow"))
        salary_max = _amount(row.get("salaryRangeHigh"))
        salary_type = _text(row.get("salaryType"))
        salary_period = (
            _SALARY_PERIODS.get(salary_type.lower())
            if salary_type and (salary_min or salary_max)
            else None
        )

        raw: dict[str, Any] = {}
        for key in ("officeName", "salaryType"):
            value = _text(row.get(key))
            if value:
                raw[key] = value

        return Job(
            url=f"{self.site_url}?jobId={job_id}",
            title=title,
            company=_text(row.get("brandName")) or self.company_name or self.site,
            ats_type=ATSType.CLEARCOMPANY,
            ats_id=job_id,
            location=location,
            country_iso=country_iso,
            is_remote=is_remote,
            department=_text(row.get("departmentName")),
            team=_text(row.get("jobFunctionName")),
            requisition_id=_text(row.get("userDefinedId")),
            apply_url=_apply_url(row.get("applyLink")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_period=salary_period,
            description=(
                _description(row.get("description"))
                if self.include_descriptions
                else None
            ),
            posted_at=_parse_date(row.get("postedDate"))
            or _parse_date(row.get("openDate")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _parse_slug(value: str) -> str:
    """The site name from ``{site}`` or a career site URL."""
    raw = (value or "").strip()
    if "://" in raw:
        parsed = urlparse(raw)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
            or (parsed.hostname or "").lower() != SITE_HOST
            or len(segments) != 1
        ):
            raise ValueError(
                f"ClearCompany career site URL must be "
                f"https://{SITE_HOST}/{{site}}, got {value!r}"
            )
        raw = segments[0]
    if not _SITE_RE.fullmatch(raw):
        raise ValueError(
            f"ClearCompany needs a career site name or "
            f"https://{SITE_HOST}/{{site}}, got {value!r}"
        )
    return raw


def _text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        cleaned = _WHITESPACE_RE.sub(" ", html.unescape(value)).strip()
        return cleaned or None
    return None


def _count(value: object) -> int | None:
    """A non-negative count, given as a number or as digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _amount(value: object) -> float | None:
    """A salary bound; zero stands for 'not given'."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = float(value) if isinstance(value, (int, float)) else float(str(value).strip())
    except ValueError:
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def _apply_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not _APPLY_HOST_RE.fullmatch(parsed.hostname or ""):
        return None
    return value.strip()


def _description(value: object) -> str | None:
    """The posting's HTML, entities unescaped; the pipeline's
    normalisation step turns it into markdown."""
    if not isinstance(value, str):
        return None
    text = html.unescape(value).strip()
    return text[:MAX_DESCRIPTION_CHARS] or None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
