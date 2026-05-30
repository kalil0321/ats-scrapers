"""VietnamWorks (https://www.vietnamworks.com) — Vietnamese jobs scraper.

VietnamWorks (operated by Navigos Group / en-Japan) is the largest
English-friendly direct-posting job platform in Vietnam (~13.7k active
postings as of May 2026). Companies publish directly through
VietnamWorks' recruiting product — postings come with structured
company / location / salary / skills data, not a free-text aggregator
feed.

Public REST API at ``https://ms.vietnamworks.com/job-search/v1.0/search``
— no auth, no key. POST with a JSON body of
``{"keyword": "", "page": N, "size": S}``. The server clamps
``size`` to 10 regardless of what's requested; pagination is driven by
``meta.nbHits`` / ``meta.nbPages`` from the first page, with a defensive
stop on the first short page in case the server returns less than
requested.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / wanted / getonbrd pattern). Output rows
carry the publishing employer's name as ``company`` so the publisher's
cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import html
import re
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_URL = "https://ms.vietnamworks.com/job-search/v1.0/search"
SITE_ROOT = "https://www.vietnamworks.com"
PER_PAGE = 50  # Requested page size — server clamps to 10 but we honour
# whatever it actually returns when terminating.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
DESCRIPTION_CAP = 10_000

# ``typeWorkingId`` → canonical ``employment_type`` enum. Mapping derived
# from the VietnamWorks help center taxonomy ("Hình thức làm việc"):
# 1 = Full-time / Toàn thời gian (the overwhelming majority of postings),
# 2 = Part-time / Bán thời gian, 3 = Contract / Hợp đồng,
# 4 = Internship / Thực tập, 5 = Temporary / Thời vụ.
_EMPLOYMENT_TYPE_MAP: dict[int, str] = {
    1: "FULL_TIME",
    2: "PART_TIME",
    3: "CONTRACT",
    4: "INTERN",
    5: "TEMPORARY",
}

# ``salaryPeriodId`` → canonical ``salary_period``. 1 = month (the
# default on VietnamWorks for monthly salaries), 2 = year. Everything
# else is left to the LLM enrichment.
_SALARY_PERIOD_MAP: dict[int, str] = {
    1: "MONTH",
    2: "YEAR",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# ASCII range plus the Vietnamese-language extra-ASCII characters that
# show up in titles that are otherwise English (đ / Đ). Anything outside
# this set in a title — i.e. any diacritic-bearing Latin char or a
# Vietnamese tone mark — is treated as evidence the listing is in
# Vietnamese.
_VIETNAMESE_DIACRITIC_RE = re.compile(
    r"["
    r"À-ɏ"          # Latin extended (covers most VN accents)
    r"Ạ-ỹ"          # Vietnamese-specific block (ạ, ậ, ệ, …)
    r"̀-ͯ"          # Combining diacritical marks
    r"]"
)


@ScraperRegistry.register(ATSType.VIETNAMWORKS)
class VietnamWorksScraper(BaseScraper):
    """VietnamWorks (vietnamworks.com) — direct postings from VN employers.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"vn"``) — the scraper enumerates the whole site.
    """

    ats = ATSType.VIETNAMWORKS

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # First page seeds the pagination: nbHits / nbPages tells us
            # how far to walk, and confirms the server-side clamped page
            # size (which we then use to decide when we've reached the
            # end if the meta numbers are missing on later pages).
            first = await self._request_page(client, sem, page=1)
            self._absorb_page(first, seen, jobs)

            meta = first.get("meta") or {}
            nb_hits = _to_int(meta.get("nbHits")) or 0
            nb_pages = _to_int(meta.get("nbPages")) or 0
            served = len(first.get("data") or [])
            if served == 0:
                return jobs
            # The server has historically clamped to 10 even when 50 is
            # requested; trust the actual first-page length over the
            # documented ``size`` so the termination check below stays
            # correct even if the limit changes.
            effective_size = served

            # When ``nb_pages`` is present, walk pages 2..nb_pages. When
            # it's missing (rare; defensive), fall back to walking until
            # a short page comes back.
            if nb_pages > 1:
                page_numbers = list(range(2, nb_pages + 1))
            else:
                page_numbers = []
                if nb_hits and effective_size:
                    expected = (nb_hits + effective_size - 1) // effective_size
                    page_numbers = list(range(2, expected + 1))

            stop = asyncio.Event()

            async def fetch_one(page: int) -> None:
                if stop.is_set():
                    return
                payload = await self._request_page(client, sem, page=page)
                served_here = len(payload.get("data") or [])
                self._absorb_page(payload, seen, jobs)
                # Short page = we're past the end. Signal sibling
                # coroutines that nothing remains to do.
                if served_here < effective_size:
                    stop.set()

            await asyncio.gather(*(fetch_one(p) for p in page_numbers))

        return jobs

    def _absorb_page(
        self,
        payload: dict[str, Any],
        seen: set[str],
        jobs: list[Job],
    ) -> None:
        for item in payload.get("data") or []:
            job = self._parse_job(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)

    # --- HTTP layer ---------------------------------------------------------

    async def _request_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        body = {"keyword": "", "page": page, "size": PER_PAGE}
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.post(
                        API_URL,
                        json=body,
                        headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"VietnamWorks fetch failed for page {page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"VietnamWorks returned non-JSON for page {page}: {exc}"
                    ) from exc
                # Out-of-range pages echo back ``{"meta": null, "data": null}``.
                # Normalise so callers can iterate cleanly.
                if not isinstance(data, dict):
                    return {"data": [], "meta": {}}
                if data.get("data") is None:
                    data["data"] = []
                if data.get("meta") is None:
                    data["meta"] = {}
                return data
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"VietnamWorks returned {response.status_code} for "
                        f"page {page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"VietnamWorks returned {response.status_code} for page {page}"
            )
        raise ScraperError(
            f"VietnamWorks exhausted retries for page {page}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("jobId")
        if raw_id is None:
            return None
        ats_id = str(raw_id)
        title = (item.get("jobTitle") or "").strip()
        if not ats_id or not title:
            return None

        company = (item.get("companyName") or "").strip() or "Unknown"

        url = _absolute_url(item.get("jobUrl"), ats_id=ats_id)

        location = _format_location(item.get("workingLocations") or [])

        # Description: combine jobDescription + jobRequirement (both HTML).
        # Falls back to the title when neither is present so the canonical
        # description field is never empty for a parsed job.
        description = _strip_html(
            _concat_text(item.get("jobDescription"), item.get("jobRequirement"))
        )
        if not description:
            description = title

        salary_currency, salary_min, salary_max = _parse_salary(item)
        salary_summary = _coerce_str(item.get("prettySalary"))
        salary_period = _SALARY_PERIOD_MAP.get(
            _to_int(item.get("salaryPeriodId")) or 0
        )

        employment_type = _EMPLOYMENT_TYPE_MAP.get(
            _to_int(item.get("typeWorkingId")) or 0
        )

        posted_at = _parse_iso(item.get("approvedOn")) or _parse_iso(
            item.get("createdOn")
        )

        language = "vi" if _VIETNAMESE_DIACRITIC_RE.search(title) else "en"

        experience = _to_int(item.get("yearsOfExperience"))

        department = _coerce_str(
            (item.get("jobFunction") or {}).get("parentName")
        )
        team_children = (item.get("jobFunction") or {}).get("children") or []
        team: str | None = None
        if team_children and isinstance(team_children, list):
            first = team_children[0]
            if isinstance(first, dict):
                team = _coerce_str(first.get("name"))

        raw: dict[str, Any] = {
            "company_id": item.get("companyId"),
            "alias": item.get("alias"),
            "expired_on": item.get("expiredOn"),
            "is_active": item.get("isActive"),
        }
        # Strip Nones so the raw payload stays compact across the CSV.
        raw = {k: v for k, v in raw.items() if v is not None}

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.VIETNAMWORKS,
            ats_id=ats_id,
            location=location,
            country_iso="VN",
            salary_currency=salary_currency,
            salary_period=salary_period,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience,
            employment_type=employment_type,
            department=department,
            team=team,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language=language,
            raw=raw or None,
        )


# --- helpers ----------------------------------------------------------------


def _absolute_url(raw: object, *, ats_id: str) -> str:
    """``jobUrl`` is normally absolute (``https://www.vietnamworks.com/...``)
    but the contract allows for a path-only form. Synthesize a canonical
    URL from the jobId as a last resort so the row stays well-formed."""
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        if s.startswith("/"):
            return f"{SITE_ROOT}{s}"
        return f"{SITE_ROOT}/{s}"
    return f"{SITE_ROOT}/job-{ats_id}-jv"


def _format_location(working_locations: list[dict[str, Any]]) -> str | None:
    """``workingLocations`` is a list of structured city entries. Prefer
    the English ``cityName`` so cross-language consumers can group; fall
    back to ``cityNameVI`` if the English one is missing. Multiple
    cities are joined with ", " (rare — most postings have a single
    location)."""
    if not isinstance(working_locations, list) or not working_locations:
        return None
    names: list[str] = []
    for loc in working_locations:
        if not isinstance(loc, dict):
            continue
        city = loc.get("cityName") or loc.get("cityNameVI")
        if isinstance(city, str) and city.strip() and city.strip() not in names:
            names.append(city.strip())
    if not names:
        return None
    return ", ".join(names[:3])  # cap to avoid pathological multi-city rows


def _parse_salary(
    item: dict[str, Any],
) -> tuple[str | None, float | None, float | None]:
    """VietnamWorks ships ``salaryMin`` / ``salaryMax`` as numeric fields
    with ``salaryCurrency`` (3-letter ISO 4217 — observed values: USD,
    VND). ``isSalaryVisible=false`` postings have both at zero — treat
    that as 'no salary disclosed' and return (None, None, None)."""
    if not item.get("isSalaryVisible", True):
        return None, None, None
    min_amount = _to_positive_float(item.get("salaryMin"))
    max_amount = _to_positive_float(item.get("salaryMax"))
    # Some listings populate only ``salary`` (a flat number) when min and
    # max are equal — fall back to it so we don't drop the structured
    # value entirely.
    if min_amount is None and max_amount is None:
        flat = _to_positive_float(item.get("salary"))
        if flat is None:
            return None, None, None
        min_amount = max_amount = flat
    currency = _coerce_str(item.get("salaryCurrency"))
    if currency and len(currency) == 3:
        return currency.upper(), min_amount, max_amount
    return None, min_amount, max_amount


def _concat_text(*parts: object) -> str:
    """Join the populated string parts with a blank line."""
    pieces: list[str] = []
    for p in parts:
        if isinstance(p, str) and p.strip():
            pieces.append(p)
    return "\n\n".join(pieces)


def _strip_html(text: str) -> str:
    """Strip tags + entities + collapse whitespace, then truncate to the
    ~10kB description budget the canonical schema documents."""
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:DESCRIPTION_CAP]


def _parse_iso(value: object) -> datetime | None:
    """VietnamWorks emits ISO-8601 with a ``+07:00`` offset
    (e.g. ``2026-04-21T17:25:42+07:00``). Python's ``fromisoformat``
    handles that natively since 3.11; we drop the tzinfo so the
    resulting datetime is comparable with the ``fetched_at`` field
    (naive UTC-ish, matching the rest of the codebase)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # Convert to UTC then drop tzinfo to keep parity with the other
        # scrapers' naive datetimes.
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _coerce_str(value: object) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _to_positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            f = float(value)
        except ValueError:
            return None
        return f if f > 0 else None
    return None
