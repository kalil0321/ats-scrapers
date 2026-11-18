"""Wanted (https://www.wanted.co.kr) — Korean + Japanese tech jobs scraper.

Wanted is the largest direct-posting tech job platform in Korea (~11k
active postings) and runs a small Japanese arm (~100 active postings).
Companies post directly through Wanted's recruiting product — not an
aggregator of LinkedIn / Indeed feeds.

Public REST API at ``https://www.wanted.co.kr/api/v4/jobs`` — no auth,
no key. Pagination is cursor-style via ``response.links.next`` until the
cursor is null. Each page returns up to 100 entries with embedded
``company`` data (no separate company-resolution fetch needed) and
``address`` already broken into ``country`` / ``location`` / ``district``
for clean location strings.

Country support is partial — only ``kr`` and ``jp`` accept the API; the
other Asian codes (sg/tw/vn/etc.) all 422. We default to scraping both
supported countries; pass ``country_codes`` to override.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / getonbrd pattern). Output rows
carry the publishing employer's name as ``company`` so the publisher's
cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._throttle import Throttle
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

logger = logging.getLogger(__name__)

API_ROOT = "https://www.wanted.co.kr"
JOBS_PATH = "/api/v4/jobs"
DETAIL_PATH_TEMPLATE = "/api/chaos/jobs/v5/{job_id}/details"
PER_PAGE = 100  # API hard-caps limit at 100 (>100 → 422).
MAX_CONCURRENCY = 4
# The WAF 403s an address's detail requests for about five minutes once it
# has sent roughly 300 of them in five minutes; blocked requests count too.
DETAIL_INTERVAL_S = 1.25

# Countries the v4 jobs endpoint accepts. The API returns 422 for codes
# outside this set (probed: sg/tw/hk/vn/my/th/id/cn/us/gb all 422 or 0).
_DEFAULT_COUNTRIES = ("kr", "jp")


@ScraperRegistry.register(ATSType.WANTED)
class WantedScraper(BaseScraper):
    """Wanted (wanted.co.kr) — direct postings from KR and JP tech companies.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper enumerates every supported country.

    To restrict to one country, instantiate with
    ``WantedScraper("any", country_codes=["kr"])``.
    """

    ats = ATSType.WANTED
    # The CloudFront WAF 403s some bot-like User-Agents, including a bare
    # ``Mozilla/5.0`` and curl's default.
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (compatible; ats-scrapers/1.0)",
        "Accept": "application/json",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        country_codes: tuple[str, ...] | list[str] = _DEFAULT_COUNTRIES,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.country_codes = tuple(country_codes)

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich_description(fetch, Throttle(1, 0), copy, asyncio.Event())
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]], country: str) -> None:
            async with lock:
                for it in items:
                    job = self._parse_job(it, country=country)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with self.make_fetcher() as fetch:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_country(cc: str) -> None:
                # Pagination is cursor-style: follow ``links.next`` until null.
                # The first request seeds the cursor with the standard params.
                url = (
                    f"{API_ROOT}{JOBS_PATH}?country={cc}"
                    f"&limit={PER_PAGE}&offset=0"
                )
                while url:
                    payload = await self._request_json(fetch, sem, url)
                    items = payload.get("data") or []
                    if not items:
                        return
                    await absorb(items, country=cc)
                    next_path = (payload.get("links") or {}).get("next")
                    if not next_path:
                        return
                    url = (
                        next_path if next_path.startswith("http")
                        else f"{API_ROOT}{next_path}"
                    )

            await asyncio.gather(*(per_country(cc) for cc in self.country_codes))
            if self.include_descriptions and jobs:
                throttle = Throttle(MAX_CONCURRENCY, DETAIL_INTERVAL_S)
                blocked = asyncio.Event()
                pending = iter(jobs)

                async def worker() -> None:
                    for job in pending:
                        if blocked.is_set():
                            return
                        await self._enrich_description(fetch, throttle, job, blocked)

                await asyncio.gather(*(worker() for _ in range(MAX_CONCURRENCY)))
                if blocked.is_set():
                    logger.warning(
                        "Wanted blocked the description requests (403); %d of %d jobs "
                        "have no description",
                        sum(1 for j in jobs if not j.description), len(jobs),
                    )
        return jobs

    async def _enrich_description(
        self,
        fetch: Fetcher,
        throttle: Throttle,
        job: Job,
        blocked: asyncio.Event,
    ) -> None:
        """Fill ``job.description``; after a 403 the remaining jobs are skipped."""
        if not job.ats_id or blocked.is_set():
            return
        url = f"{API_ROOT}{DETAIL_PATH_TEMPLATE.format(job_id=job.ats_id)}"
        async with throttle:
            if blocked.is_set():
                return
            try:
                response = await fetch.request("GET", url, handled={403})
                payload = response.json() if response.status_code != 403 else {}
            except (ScraperError, ValueError):
                return
        if response.status_code == 403:
            blocked.set()
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data", payload)
        posting = data.get("job") if isinstance(data, dict) else None
        if not isinstance(posting, dict):
            return
        if posting.get("id") is not None and str(posting["id"]) != job.ats_id:
            return
        detail = posting.get("detail")
        if not isinstance(detail, dict):
            return
        description = _compose_description(detail, posting.get("employment_note"))
        if description and not job.description:
            job.description = description[:25_000]

    # --- HTTP layer ---------------------------------------------------------

    async def _request_json(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        url: str,
    ) -> dict[str, Any]:
        async with sem:
            # 422 = API rejected the params (unsupported country,
            # oversize limit). Treat as "this slice has no data".
            response = await fetch.request("GET", url, handled={422})
        if response.status_code == 422:
            return {"data": [], "links": {}}
        return response.json()

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, item: dict[str, Any], *, country: str) -> Job | None:
        # ``or ""`` would coerce id=0 to empty — use an explicit None check
        # so any genuine zero (not currently in the API but cheap to guard)
        # round-trips correctly.
        raw_id = item.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id)
        title = (item.get("position") or "").strip()
        if not ats_id or not title:
            return None

        company_obj = item.get("company") or {}
        company_name = (company_obj.get("name") or "").strip() or "Unknown"
        company_id = str(company_obj.get("id") or "") or None

        location = _format_location(item.get("address") or {})

        # ``annual_from`` / ``annual_to`` is the years-of-experience range,
        # not a salary range — keep ``annual_from`` on the canonical
        # ``experience`` field (minimum required years) and stash the
        # max in ``raw`` so we don't lose the upper bound.
        experience = _to_int(item.get("annual_from"))
        annual_to = _to_int(item.get("annual_to"))

        # Posting timestamps: the v4 listing payload doesn't surface
        # ``confirm_time`` / ``create_time`` (those come from v1 only,
        # which doesn't paginate) — leave posted_at None rather than
        # invent a timestamp.
        raw: dict[str, Any] = {}
        if annual_to is not None:
            raw["annual_to"] = annual_to
        if company_obj.get("industry_name"):
            raw["industry_name"] = company_obj["industry_name"]
        if company_id:
            raw["company_id"] = company_id
        full_loc = (item.get("address") or {}).get("full_location")
        if isinstance(full_loc, str) and full_loc.strip():
            raw["full_location"] = full_loc.strip()
        cat_tags = item.get("category_tags")
        if isinstance(cat_tags, list) and cat_tags:
            raw["category_tag_ids"] = [
                t.get("id") for t in cat_tags
                if isinstance(t, dict) and t.get("id") is not None
            ]
        raw["country"] = country.upper()

        return Job(
            url=f"{API_ROOT}/wd/{ats_id}",
            title=title,
            company=company_name,
            ats_type=ATSType.WANTED,
            ats_id=ats_id,
            location=location,
            experience=experience,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _format_location(address: dict[str, Any]) -> str | None:
    """Wanted's ``address`` ships with explicit ``country`` / ``location`` /
    ``district`` fields, all in the listing language (Korean for KR,
    Japanese for JP). Combine the populated ones into a comma-separated
    string. Falls back to ``full_location`` when the structured parts are
    empty (rare).
    """
    parts: list[str] = []
    for key in ("district", "location", "country"):
        value = address.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    if parts:
        return ", ".join(parts)
    full = address.get("full_location")
    if isinstance(full, str) and full.strip():
        return full.strip()
    return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _compose_description(detail: dict[str, Any], employment_note: object = None) -> str | None:
    parts: list[str] = []
    for key in (
        "intro",
        "main_tasks",
        "requirements",
        "preferred_points",
        "benefits",
        "hire_rounds",
    ):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if isinstance(employment_note, str) and employment_note.strip():
        parts.append(employment_note.strip())
    text = "\n\n".join(parts).strip()
    return text or None
