"""JobThai (https://www.jobthai.com) — Thailand's largest Thai-language job board.

JobThai is the dominant direct-posting job board in Thailand (separate
from JobsDB TH, which is SEEK-owned). Coverage spans every sector, not
just tech — at probe time (2026-05) the API reported ~48k active
postings.

GraphQL API at ``https://api.jobthai.com/v1/graphql`` — no auth, no
key. The site is a Next.js SPA backed by Apollo; the same endpoint
serves all reads. Introspection is disabled in production but the
default Apollo error messages leak field/type suggestions ("Did you
mean …") so the schema is recoverable by trial.

Two quirks shape the fetch plan:

  - Elasticsearch ``from + size <= 10000`` cap. The ``searchJobs``
    query is backed by ES; the unfiltered listing tops out at 10 000
    even though the API reports ``total ≈ 48k``. Sharding by
    ``jobtype`` (43 distinct categories) keeps every per-bucket
    sweep well under the cap — the busiest type at probe time was
    "Sales" at ~7.2k. Results are deduped by job ``id`` across buckets
    (a posting can be tagged with multiple types, though we observed
    very little overlap in practice).

  - Apollo CSRF guard. Bare POSTs are rejected unless either a
    non-form ``Content-Type`` is set (we use ``application/json``)
    or one of the preflight headers (``apollo-require-preflight`` /
    ``x-apollo-operation-name``) is present. We set both for safety.

Single-source scraper: ``company_slug`` is informational and ignored
(matches the bundesagentur / eures / wanted / jobsch pattern). Output
rows carry the publishing employer's Thai-language name as ``company``.
The dataset is overwhelmingly Thai-language; ``language="th"`` is set
on every row and ``country_iso="TH"`` on every row.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.jobthai.com/v1/graphql"
JOB_URL_TEMPLATE = "https://www.jobthai.com/en/job/{id}"
PER_PAGE = 100  # ES ``from+size<=10000`` cap → 100 pages × 100 rows.
MAX_USABLE_PAGES = 100  # 100 × 100 = 10 000 = ES window cap.
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# ``searchJobs`` query — keep the selection set conservative since the
# schema is reconstructed by probing; new fields can be added once
# verified. ``$jobtype`` is a String (the API rejects [Int!]).
_SEARCH_JOBS_QUERY = (
    "query searchJobs($page:Int,$size:Int,$jobtype:String){"
    "searchJobs(filter:{page:$page,size:$size,jobtype:$jobtype}){"
    "data{"
    "total "
    "data{"
    "id jobTitle companyName companyID workLocation salary "
    "jobDescription updatedAt tags "
    "urgent{id name} "
    "jobType{id name} "
    "province{id name} "
    "district{id name}"
    "}}}}"
)
_JOB_TYPES_QUERY = (
    "query getJobTypeList{getJobTypeList(version:1){data{id}}}"
)

# Salary string → currency hints. JobThai salaries are nearly always
# in Thai baht (THB); the API surfaces them as free text mixing Thai
# digits / Arabic digits and a trailing "บาท" (baht) marker. Anything
# else falls through and ``salary_currency`` stays None.
_THAI_BAHT_RE = re.compile(r"บาท|THB|baht", re.IGNORECASE)
_NEGOTIABLE_RE = re.compile(r"ตามตกลง|ตามโครงสร้าง|ตามประสบการณ์|ตามตำแหน่ง|negotiable", re.IGNORECASE)
# Salary range pattern: numbers, optional commas/dots, separator
# (hyphen, en-dash, "to", "ถึง"), more numbers. Captures min/max.
_RANGE_RE = re.compile(
    r"(?P<min>\d[\d,\.]*)\s*(?:[-–~]|to|ถึง)\s*"
    r"(?P<max>\d[\d,\.]*)",
    re.IGNORECASE,
)
# Single-value salary pattern (no range), trailing baht / THB.
_SINGLE_RE = re.compile(r"(?P<val>\d[\d,\.]*)")

# Thai-script detection: U+0E00..U+0E7F covers the Thai block. If any
# character in a title falls in this range we tag the listing as
# ``language="th"``; otherwise ``language="en"`` (rare — most postings
# are Thai even when the title is partly English).
_THAI_SCRIPT_RE = re.compile(r"[฀-๿]")
_SALARY_PERIOD_PATTERNS = (
    (re.compile(r"เดือน|/\s*month|per\s+month|monthly", re.IGNORECASE), "MONTH"),
    (re.compile(r"วัน|/\s*day|per\s+day|daily", re.IGNORECASE), "DAY"),
    (re.compile(r"ชั่วโมง|/\s*hour|per\s+hour|hourly", re.IGNORECASE), "HOUR"),
)


@ScraperRegistry.register(ATSType.JOBTHAI)
class JobThaiScraper(BaseScraper):
    """JobThai (jobthai.com) — Thailand's largest direct-posting board.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``) — the scraper enumerates the entire site by
    sharding across the ``jobtype`` filter to defeat the 10 000-row
    Elasticsearch window cap on a single query.

    To restrict to a subset of categories (smoke tests, partial
    refreshes), instantiate with
    ``JobThaiScraper("any", job_type_ids=("4",))``.
    """

    ats = ATSType.JOBTHAI

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        job_type_ids: tuple[str, ...] | list[str] | None = None,
        max_pages_per_type: int | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.job_type_ids = tuple(job_type_ids) if job_type_ids is not None else None
        self.max_pages_per_type = max_pages_per_type or MAX_USABLE_PAGES
        self._full_catalogue = max_pages_per_type is None

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            job_type_ids = self.job_type_ids
            if job_type_ids is None:
                job_type_ids = await self._discover_job_types(client, sem)

            async def per_type(jt: str) -> None:
                for page in range(1, self.max_pages_per_type + 1):
                    payload = await self._search_page(client, sem, jt, page)
                    data_envelope = (payload.get("searchJobs") or {}).get("data") or {}
                    items = data_envelope.get("data") or []
                    total = data_envelope.get("total")
                    if page == 1:
                        if not isinstance(total, int) or total < 0:
                            raise ScraperError(
                                f"JobThai jobtype={jt} omitted a valid total"
                            )
                        if self._full_catalogue and total > (
                            self.max_pages_per_type * PER_PAGE
                        ):
                            raise ScraperError(
                                f"JobThai jobtype={jt} has {total} jobs, above "
                                "the validated Elasticsearch window"
                            )
                    if not items:
                        return
                    async with lock:
                        for item in items:
                            job = self._parse_job(item)
                            if job is None or job.ats_id in seen:
                                continue
                            seen.add(job.ats_id)
                            jobs.append(job)
                    # Short-circuit when the current page returned less
                    # than a full page — the next page is guaranteed
                    # empty.
                    if len(items) < PER_PAGE:
                        return

            await asyncio.gather(*(per_type(jt) for jt in job_type_ids))
        return jobs

    async def _discover_job_types(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
    ) -> tuple[str, ...]:
        payload = await self._graphql(
            client,
            sem,
            operation_name="getJobTypeList",
            query=_JOB_TYPES_QUERY,
            variables={},
        )
        envelope = payload.get("getJobTypeList")
        rows = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(rows, list):
            raise ScraperError("JobThai job-type discovery returned invalid data")
        ids = tuple(
            str(row["id"])
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None
        )
        if not ids:
            raise ScraperError("JobThai job-type discovery returned no IDs")
        return ids

    # --- HTTP layer ---------------------------------------------------------

    async def _search_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        jobtype: str,
        page: int,
    ) -> dict[str, Any]:
        return await self._graphql(
            client,
            sem,
            operation_name="searchJobs",
            query=_SEARCH_JOBS_QUERY,
            variables={"page": page, "size": PER_PAGE, "jobtype": jobtype},
            context=f"jobtype={jobtype} page={page}",
        )

    async def _graphql(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        context: str | None = None,
    ) -> dict[str, Any]:
        label = context or operation_name
        body = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Apollo CSRF guard requires either a non-form Content-Type
            # OR one of these preflight headers. Set both so a future
            # tightening on either side doesn't break us.
            "apollo-require-preflight": "true",
            "x-apollo-operation-name": operation_name,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with sem:
                    response = await client.post(
                        GRAPHQL_URL, json=body, headers=headers,
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"JobThai fetch failed ({label}): {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"JobThai returned non-JSON for {label}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ScraperError(
                        f"JobThai returned invalid payload for {label}"
                    )
                if payload.get("errors"):
                    # Treat GraphQL errors as fatal — they signal a
                    # schema drift, not a transient failure.
                    raise ScraperError(
                        f"JobThai GraphQL errors ({label}): {payload['errors']}"
                    )
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ScraperError(
                        f"JobThai returned invalid data for {label}"
                    )
                return data
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"JobThai returned {response.status_code} for {label} after "
                        f"{MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"JobThai returned {response.status_code} for {label}"
            )
        raise ScraperError(
            f"JobThai exhausted retries for {label}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        if raw_id is None:
            return None
        ats_id = str(raw_id)
        title = (item.get("jobTitle") or "").strip()
        if not ats_id or not title:
            return None

        company_name = (item.get("companyName") or "").strip() or "Unknown"
        company_id = item.get("companyID")

        location = _format_location(item)
        description = _format_description(item.get("jobDescription"))
        posted_at = _parse_iso(item.get("updatedAt"))

        salary_summary, salary_currency, salary_min, salary_max = _parse_salary(
            item.get("salary")
        )

        # Heuristic language detection: any Thai-script char in the
        # title → ``th``. Otherwise ``en``. Most postings are Thai;
        # the few all-English titles are usually for international
        # firms posting in English.
        language = "th" if _THAI_SCRIPT_RE.search(title) else "en"

        # Provider-specific overflow — keep only IDs and labels we
        # parsed off the listing. JobThai's category / industry /
        # level fields are not on JobSearch (only on the per-job
        # detail), so the task-spec ``category``/``industry``/``level``
        # are best-effort: ``category`` is the ``jobType.id``.
        raw: dict[str, Any] = {}
        job_type = item.get("jobType") or {}
        if job_type.get("id") is not None:
            raw["category"] = job_type.get("id")
        if job_type.get("name"):
            raw["category_name"] = job_type.get("name")
        province = item.get("province") or {}
        if province.get("id") is not None:
            raw["province_id"] = province.get("id")
        if province.get("name"):
            raw["province_name"] = province.get("name")
        district = item.get("district") or {}
        if district.get("id") is not None:
            raw["district_id"] = district.get("id")
        if district.get("name"):
            raw["district_name"] = district.get("name")
        urgent = item.get("urgent") or {}
        urgent_id = urgent.get("id")
        if isinstance(urgent_id, int) and urgent_id > 0:
            raw["is_urgent"] = True
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            raw["tags"] = [t for t in tags if isinstance(t, str)]
        if company_id is not None:
            raw["company_id"] = company_id
        work_location = item.get("workLocation")
        if isinstance(work_location, str) and work_location.strip():
            raw["work_location"] = work_location.strip()

        return Job(
            url=JOB_URL_TEMPLATE.format(id=ats_id),
            title=title,
            company=company_name,
            ats_type=ATSType.JOBTHAI,
            ats_id=ats_id,
            location=location,
            country_iso="TH",
            region="Asia",
            language=language,
            description=description,
            posted_at=posted_at,
            salary_currency=salary_currency,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_period=_salary_period(item.get("salary")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


# --- helpers ---------------------------------------------------------------


def _format_location(item: dict[str, Any]) -> str | None:
    """Build a comma-separated location from district + province +
    free-text ``workLocation``. JobThai stores the province name in
    Thai script (e.g. ``กรุงเทพมหานคร``); we keep it verbatim — the
    downstream LLM enrichment knows how to handle multilingual
    location strings."""
    parts: list[str] = []
    district = item.get("district") or {}
    if isinstance(district, dict) and district.get("name"):
        parts.append(str(district["name"]).strip())
    province = item.get("province") or {}
    if isinstance(province, dict) and province.get("name"):
        name = str(province["name"]).strip()
        if name and name not in parts:
            parts.append(name)
    work_loc = (item.get("workLocation") or "").strip()
    # The free-text ``workLocation`` is often a paragraph (address,
    # transport directions, emoji-heavy benefits blurb). Skip it for
    # the structured ``location`` field — it would blow past the
    # ~120-char expectation downstream consumers have. The full text
    # is preserved in ``description`` via _format_description below.
    if not parts and work_loc:
        # Fall back to the first line of the free-text field when no
        # structured parts are present.
        first_line = work_loc.split("\n", 1)[0].strip()
        if first_line:
            return first_line
    return ", ".join(parts) if parts else None


def _format_description(value: object) -> str | None:
    """JobThai's ``jobDescription`` is a list of bullet-style strings.
    Join with newlines to produce a plain-text description; cap at
    ~10 kB to match the canonical schema's truncation policy."""
    if isinstance(value, list):
        lines = [str(v).strip() for v in value if str(v).strip()]
        if not lines:
            return None
        joined = "\n".join(lines)
        return joined[:10_000]
    if isinstance(value, str):
        stripped = value.strip()
        return stripped[:10_000] if stripped else None
    return None


def _parse_iso(value: object) -> datetime | None:
    """JobThai timestamps come in ISO 8601 with a trailing ``Z``
    (e.g. ``2026-05-12T04:09:54.000Z``). ``fromisoformat`` accepts
    ``+00:00`` but not ``Z`` until 3.11; we normalize unconditionally."""
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        log.debug("JobThai: failed to parse timestamp %r", value)
        return None


def _parse_salary(
    value: object,
) -> tuple[str | None, str | None, float | None, float | None]:
    """Parse JobThai's free-text salary field.

    Returns ``(summary, currency, min, max)``. JobThai salaries are
    almost universally in Thai baht; the field text mixes Thai script
    ("บาท"), Arabic digits, hyphens / en-dashes, and free-form
    fallback phrases like ``ตามตกลง`` (negotiable).

    Examples observed in production:

      - ``"15,000 - 20,000 บาท"`` → THB, 15000, 20000
      - ``"35,000 - 50,000 บาท"`` → THB, 35000, 50000
      - ``"ตามตกลง"`` → currency None, min/max None, summary preserved
      - ``"ตามโครงสร้างบริษัทฯ"`` → currency None, min/max None
    """
    if not isinstance(value, str):
        return None, None, None, None
    summary = value.strip()
    if not summary:
        return None, None, None, None

    has_baht = bool(_THAI_BAHT_RE.search(summary))
    is_negotiable = bool(_NEGOTIABLE_RE.search(summary))

    if is_negotiable and not has_baht:
        # Pure free-text "negotiable" — keep the original phrase but
        # don't invent a currency.
        return summary, None, None, None

    range_match = _RANGE_RE.search(summary)
    if range_match:
        min_v = _to_float(range_match.group("min"))
        max_v = _to_float(range_match.group("max"))
        currency = "THB" if has_baht else None
        return summary, currency, min_v, max_v

    single_match = _SINGLE_RE.search(summary)
    if single_match and has_baht:
        val = _to_float(single_match.group("val"))
        return summary, "THB", val, val

    # Anything else: keep the summary verbatim, no currency claim.
    return summary, None, None, None


def _to_float(text: str) -> float | None:
    if not isinstance(text, str):
        return None
    cleaned = text.replace(",", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _salary_period(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    for pattern, period in _SALARY_PERIOD_PATTERNS:
        if pattern.search(value):
            return period
    return None
