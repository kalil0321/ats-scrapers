"""Hosco (https://www.hosco.com) — global hospitality jobs board.

Hosco is the leading hospitality-industry job platform, with strong
coverage of the MENA region (UAE, Saudi Arabia, Qatar, …), Europe, and
South-East Asia. Roughly 5.5k live postings at any given time —
front-of-house, back-of-house, F&B, hotel management, cruise lines.

Single-source scraper: ``company_slug`` is informational and ignored.
The site is a Next.js app that exposes a public, no-auth JSON data
endpoint at ``/_next/data/{buildId}/en/jobs.json``. The ``buildId``
changes on every Hosco deploy, so the scraper:

  1. Fetches the public ``/en/jobs`` HTML page once at the start.
  2. Extracts the current ``buildId`` via the embedded
     ``"buildId":"..."`` JSON literal Next.js writes into the markup.
  3. Calls ``/_next/data/{buildId}/en/jobs.json?page=N`` for pagination,
     reusing the discovered buildId.

If a mid-crawl page returns 404, the buildId has very likely rotated
because Hosco redeployed mid-scrape — the scraper re-discovers it once
and retries that page. Otherwise pagination stops when the response
list is shorter than ``PAGE_SIZE`` or we've covered the reported
``count``.

The search response is summary-only; the description body is the
``excerpt`` field. Deeper detail (full description, perks, …) would
require N per-job requests we don't make here — that's a future
enrichment pass.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://www.hosco.com"
JOBS_HTML_URL = f"{BASE_URL}/en/jobs"
PAGE_SIZE = 10  # Hosco's fixed search page size — keep in sync with the API.
DEFAULT_MAX_PAGES = 1_000  # 5.5k / 10 ≈ 550, give plenty of headroom.
MAX_CONCURRENCY = 4
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.5

BROWSER_UA: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
        "Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
JSON_HEADERS: dict[str, str] = {
    "User-Agent": BROWSER_UA["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_BUILD_ID_RE = re.compile(r'"buildId":"([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")

# Hosco's ``types`` field values → canonical ``EmploymentType`` enum.
# Values are lowercased/slug-ified before lookup so spacing/casing
# variants ("Full-Time", "full time", "FULL_TIME") all match.
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "full-time": "FULL_TIME",
    "full_time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
    "part-time": "PART_TIME",
    "part_time": "PART_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "freelance": "CONTRACT",
    "seasonal": "TEMPORARY",
    "temporary": "TEMPORARY",
    "graduate-program": "INTERN",
    "graduate_program": "INTERN",
    "internship": "INTERN",
    "intern": "INTERN",
    "apprenticeship": "INTERN",
    "trainee": "INTERN",
}


@ScraperRegistry.register(ATSType.HOSCO)
class HoscoScraper(BaseScraper):
    """Hosco (hosco.com) — global hospitality job board.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"global"``) — the scraper enumerates the
    entire ``/en/jobs`` directory.
    """

    ats = ATSType.HOSCO

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            build_id = await self._discover_build_id(client)
            # Page 1 also tells us the total count — fetch sequentially
            # so we can size the concurrent fan-out correctly.
            first = await self._fetch_page(client, sem, build_id, page=1)
            results = _extract_results(first)
            count = _extract_count(first)

            seen: set[str] = set()
            jobs: list[Job] = []
            for item in results:
                job = self._parse_job(item)
                if job is None or job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                jobs.append(job)

            if count is not None:
                total_pages = min(
                    self.max_pages, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
                )
            elif len(results) < PAGE_SIZE:
                total_pages = 1
            else:
                total_pages = self.max_pages

            if total_pages > 1:
                build_id_holder = [build_id]
                tasks = [
                    self._fetch_page_with_refresh(
                        client, sem, build_id_holder, page=p
                    )
                    for p in range(2, total_pages + 1)
                ]
                for coro in asyncio.as_completed(tasks):
                    payload = await coro
                    if not payload:
                        continue
                    for item in _extract_results(payload):
                        job = self._parse_job(item)
                        if job is None or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _discover_build_id(self, client: httpx.AsyncClient) -> str:
        """Fetch the public listing page and pull the current Next.js
        ``buildId`` out of the embedded ``__NEXT_DATA__`` script.

        Raises :class:`ScraperError` if the page returns non-200 or the
        regex doesn't match — both indicate a layout change worth alerting
        on rather than silently producing an empty result.
        """
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(JOBS_HTML_URL, headers=BROWSER_UA)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Hosco buildId discovery failed: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                match = _BUILD_ID_RE.search(response.text)
                if not match:
                    raise ScraperError(
                        "Hosco buildId not found — site layout changed"
                    )
                return match.group(1)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Hosco buildId discovery got {response.status_code} "
                        f"after {MAX_RETRIES} retries"
                    )
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))
                continue
            raise ScraperError(
                f"Hosco buildId discovery got {response.status_code}"
            )
        raise ScraperError(
            f"Hosco buildId discovery exhausted retries: {last_exc}"
        )

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        build_id: str,
        *,
        page: int,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}/_next/data/{build_id}/en/jobs.json"
        params = {"page": page}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        url, params=params, headers=JSON_HEADERS,
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Hosco fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Hosco returned non-JSON at page={page}: {exc}"
                    ) from exc
            if response.status_code == 404:
                # Signal to the caller that the buildId is stale; let it
                # decide whether to re-discover and retry.
                return {"__hosco_stale_build_id__": True}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Hosco returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2**attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Hosco returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"Hosco exhausted retries at page={page}: {last_exc}"
        )

    async def _fetch_page_with_refresh(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        build_id_holder: list[str],
        *,
        page: int,
    ) -> dict[str, Any]:
        """Fetch a page, re-discovering the buildId once on 404.

        Hosco occasionally redeploys mid-scrape, which invalidates the
        ``/_next/data/{buildId}/...`` URLs we hold. A single recovery
        attempt covers that case without retry-storming the homepage on
        every page failure.
        """
        payload = await self._fetch_page(client, sem, build_id_holder[0], page=page)
        if payload.get("__hosco_stale_build_id__"):
            new_id = await self._discover_build_id(client)
            build_id_holder[0] = new_id
            payload = await self._fetch_page(client, sem, new_id, page=page)
            if payload.get("__hosco_stale_build_id__"):
                # Two consecutive 404s — give up on this page rather than
                # spinning. The next full run will pick it up.
                return {}
        return payload

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        if raw_id in (None, ""):
            return None
        ats_id = str(raw_id)

        title = (item.get("title") or "").strip()
        if not title:
            return None

        href = item.get("url") or ""
        if isinstance(href, str) and href.startswith("/"):
            url = f"{BASE_URL}{href}"
        elif isinstance(href, str) and href.startswith("http"):
            url = href
        else:
            url = f"{BASE_URL}/en/jobs/{ats_id}"

        company_obj = item.get("company") or {}
        company = (
            company_obj.get("name").strip()
            if isinstance(company_obj, dict)
            and isinstance(company_obj.get("name"), str)
            and company_obj.get("name").strip()
            else "Unknown"
        )

        raw_location = item.get("displayed_location")
        location = (
            raw_location.strip() or None
            if isinstance(raw_location, str)
            else None
        )

        description = _strip_html(item.get("excerpt") or "")
        if not description:
            description = None

        pay_range = item.get("pay_range") or {}
        salary_min = _to_float(_first_present(pay_range, ("min", "minimum")))
        salary_max = _to_float(_first_present(pay_range, ("max", "maximum")))
        salary_currency = _normalize_currency(pay_range.get("currency"))
        if salary_currency is None and (salary_min is not None or salary_max is not None):
            # We have a numeric range but no currency — drop the range
            # rather than guessing; the canonical schema treats currency
            # as required context for salary numbers.
            salary_min = None
            salary_max = None

        employment_type = _map_employment_type(item.get("types"))

        raw: dict[str, Any] = {}
        slug = item.get("slug")
        if isinstance(slug, str) and slug:
            raw["slug"] = slug
        start_date = item.get("start_date")
        if start_date:
            raw["start_date"] = start_date
        owner = item.get("owner")
        if isinstance(owner, dict) and owner.get("type"):
            raw["owner_kind"] = owner.get("type")
        types = item.get("types")
        if isinstance(types, list) and types:
            raw["types"] = types

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.HOSCO,
            ats_id=ats_id,
            location=location,
            salary_currency=salary_currency,
            salary_period="YEAR" if salary_currency else None,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            description=description,
            posted_at=_parse_date(item.get("posted_date")),
            fetched_at=datetime.now(),
            language="en",
            raw=raw or None,
        )


# --- helpers ----------------------------------------------------------------


def _extract_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    search = (
        ((payload.get("pageProps") or {}).get("initialState") or {})
        .get("jobDirectory")
        or {}
    ).get("search") or {}
    results = search.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def _extract_count(payload: dict[str, Any]) -> int | None:
    search = (
        ((payload.get("pageProps") or {}).get("initialState") or {})
        .get("jobDirectory")
        or {}
    ).get("search") or {}
    count = search.get("count")
    if isinstance(count, int) and count >= 0:
        return count
    return None


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return num if num > 0 else None
    return None


def _normalize_currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) == 3 and code.isalpha():
        return code
    return None


def _map_employment_type(value: Any) -> str | None:
    candidate = value[0] if isinstance(value, list) and value else value
    if not isinstance(candidate, str):
        return None
    key = candidate.strip().lower().replace(" ", "-")
    return _EMPLOYMENT_TYPE_MAP.get(key)


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Hosco gives plain ``YYYY-MM-DD`` in the search payload; tolerate a
    # full ISO timestamp too in case detail pages start to surface one.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:10_000]
