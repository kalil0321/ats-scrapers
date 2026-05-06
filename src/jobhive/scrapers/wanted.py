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
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_ROOT = "https://www.wanted.co.kr"
JOBS_PATH = "/api/v4/jobs"
PER_PAGE = 100  # API hard-caps limit at 100 (>100 → 422).
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

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

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        country_codes: tuple[str, ...] | list[str] = _DEFAULT_COUNTRIES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.country_codes = tuple(country_codes)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
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

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_country(cc: str) -> None:
                # Pagination is cursor-style: follow ``links.next`` until null.
                # The first request seeds the cursor with the standard params.
                url = (
                    f"{API_ROOT}{JOBS_PATH}?country={cc}"
                    f"&limit={PER_PAGE}&offset=0"
                )
                while url:
                    payload = await self._request_json(client, sem, url)
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
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Wanted fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Wanted returned non-JSON for {url}: {exc}"
                    ) from exc
            if response.status_code == 422:
                # API rejected the params (unsupported country, oversize
                # limit). Treat as "this slice has no data".
                return {"data": [], "links": {}}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Wanted returned {response.status_code} for "
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
                f"Wanted returned {response.status_code} for {url}"
            )
        raise ScraperError(
            f"Wanted exhausted retries for {url}: {last_exc}"
        )

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
            fetched_at=datetime.now(),
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
