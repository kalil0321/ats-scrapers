"""HireHive career sites (``{company}.hirehive.com``).

Every company has its own HireHive host, and the host answers a public JSON
API with no key:

    GET https://{company}.hirehive.com/api/v2/jobs?page={n}&page_size=100

Pages hold at most 100 jobs. A page carries the whole list's size
(``meta.total_items``) and whether another page follows
(``meta.has_next_page``). Each job carries its description (``html`` and
``text``), its hosted address, the place as a city, a state code and a country
(name and code), a category, the employment type and, when the employer fills
them in, a salary line. An unknown company is HTTP 404.

The API does not name the company. The home page of the site does
(``Jobs at {company}``), and is read once; a site whose home page cannot be
read keeps the company label as its name.

The list is complete when the jobs read equal ``total_items``; otherwise the
scraper raises rather than return part of a board.

``company_slug`` is the label before ``.hirehive.com``. ``ats_id`` is the job
id (``job_BnEK0Y``).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._job_places import job_location
from ats_scrapers.scrapers._posting_detail import body_text
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers._source_timestamp import source_timestamp
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 100
# A runaway guard only: 1,000 pages would be 100,000 jobs.
MAX_PAGES = 1_000
MAX_DESCRIPTION_CHARS = 25_000

_COMPANY_RE = re.compile(r"<title>[^<]*?\bJobs at\s+(.*?)\s*(?:\|[^<]*)?</title>", re.IGNORECASE | re.DOTALL)
_EMPLOYMENT: dict[str, EmploymentType] = {
    "fulltime": "FULL_TIME", "parttime": "PART_TIME", "contract": "CONTRACT",
    "temporary": "TEMPORARY", "internship": "INTERN", "intern": "INTERN",
}


@ScraperRegistry.register(ATSType.HIREHIVE)
class HireHiveScraper(BaseScraper):
    """All open jobs of one HireHive career site."""

    ats = ATSType.HIREHIVE

    def __init__(self, company_slug: str, **kwargs: Any) -> None:
        super().__init__(company_slug, **kwargs)
        self.company_slug = require_host_label(company_slug, provider="HireHiveScraper").lower()

    @property
    def _base(self) -> str:
        return f"https://{self.company_slug}.hirehive.com"

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            items, total = await self._read_all(fetch)
            company = await self._company(fetch)
        jobs: dict[str, Job] = {}
        for item in items:
            job = self._job(item, company)
            if job is not None:
                jobs.setdefault(job.ats_id or "", job)
        if total is not None and len(jobs) != total:
            raise ScraperError(
                f"HireHive ({self.company_slug}): read {len(jobs)} jobs but the site counts {total}",
            )
        return list(jobs.values())

    async def _read_all(self, fetch: Fetcher) -> tuple[list[dict[str, Any]], int | None]:
        items: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            data = await fetch.get_json(f"{self._base}/api/v2/jobs?page={page}&page_size={PAGE_SIZE}")
            meta = data.get("meta") if isinstance(data, dict) else None
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(meta, dict) or not isinstance(rows, list) \
                    or not isinstance(meta.get("total_items"), int):
                raise ScraperError(f"HireHive ({self.company_slug}) returned an unrecognized page")
            total = meta["total_items"]
            items.extend(row for row in rows if isinstance(row, dict))
            if meta.get("has_next_page") is not True or not rows:
                return items, total
        raise ScraperError(f"HireHive ({self.company_slug}) did not end after {MAX_PAGES} pages")

    async def _company(self, fetch: Fetcher) -> str:
        try:
            page = await fetch.get_text(f"{self._base}/")
        except ScraperError:
            return self.company_slug
        found = _COMPANY_RE.search(page)
        name = " ".join(html.unescape(found.group(1)).split()) if found else ""
        return name or self.company_slug

    def _job(self, item: dict[str, Any], company: str) -> Job | None:
        job_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not job_id or not title:
            return None
        country: dict[str, Any] = item["country"] if isinstance(item.get("country"), dict) else {}
        place = job_location(
            city=item.get("location"), region=item.get("state_code"),
            country=country.get("name"), country_code=country.get("code"),
        )
        category: dict[str, Any] = item["category"] if isinstance(item.get("category"), dict) else {}
        kind: dict[str, Any] = item["type"] if isinstance(item.get("type"), dict) else {}
        language: dict[str, Any] = item["language"] if isinstance(item.get("language"), dict) else {}
        description = item.get("description")
        description = (description.get("html") or description.get("text")) if isinstance(description, dict) else description
        url = str(item.get("hosted_url") or f"{self._base}/")
        salary = item.get("salary")
        raw = {"experience": item["experience"]["type"]} if isinstance(item.get("experience"), dict) \
            and item["experience"].get("type") not in (None, "NA") else None
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=self.ats,
            ats_id=job_id,
            location=place.label if place else None,
            locations=[place] if place else None,
            country_iso=place.country_iso if place else None,
            job_category=category.get("name") or None,
            employment_type=_EMPLOYMENT.get(str(kind.get("type") or "").lower()),
            salary_summary=salary.strip() or None if isinstance(salary, str) else None,
            apply_url=url,
            description=body_text(description)[:MAX_DESCRIPTION_CHARS] or None,
            posted_at=_instant(item.get("published_date")),
            source_created_at=source_timestamp(item.get("created_date")),
            language=str(language.get("code") or "").split("-")[0].lower() or None,
            fetched_at=datetime.now(UTC),
            raw=raw,
        )


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
