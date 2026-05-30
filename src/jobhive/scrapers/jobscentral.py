"""JobsCentral SG (https://jobscentral.com.sg) — Singapore jobs board.

JobsCentral is one of Singapore's longest-running general-audience job
boards (finance / engineering / sales / education / …). Owned by the
SEEK-affiliated CXS Asia group; the SG site is a Next.js SPA that
embeds the live job list into the page via Next's ``__NEXT_DATA__``
SSR payload.

Source: ``GET https://jobscentral.com.sg/jobs`` — the HTML response
inlines a ``<script id="__NEXT_DATA__">`` JSON blob with
``props.pageProps.jobs.items`` containing the full structured row for
every listing on the page (id, title, company, category, seniority,
short description, location, employment type, remote flag, …). No
follow-up detail fetch is needed for the canonical schema.

Pagination: the URL accepts ``?page=N``; the embedded
``pageProps.jobs.count`` reports the total, ``jobSearchModel.limit``
the per-page (default 50). The active board is small (low-hundreds at
any one time) — typically one page covers everything.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://jobscentral.com.sg"
LISTING_URL = f"{BASE_URL}/jobs"
MAX_CONCURRENCY = 3
MAX_RETRIES = 3
MAX_PAGES_DEFAULT = 50  # 50 * 50 = 2.5k jobs — far above the actual board.
RETRY_BASE_DELAY = 1.5

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# JobsCentral ``occupationType`` enum → canonical employment_type.
_EMPLOYMENT_MAP: dict[str, str] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "INTERNSHIP": "INTERN",
    "INTERN": "INTERN",
    "TEMPORARY": "TEMPORARY",
    "FREELANCE": "CONTRACT",
}

# Category enum → URL slug. JobsCentral builds canonical detail URLs as
# ``/jobs/{category-slug}/{id}``, but the router accepts any slug for
# a given id — so this map is best-effort cosmetic. Unknown values fall
# back to a generic lowercased form.
_CATEGORY_SLUGS: dict[str, str] = {
    "ADMINISTRATIVE_SECRETARIAL": "administrative-or-secretarial-jobs",
    "CALL_CENTER_CUSTOMER_SUPPORT": "call-center-or-customer-support-jobs",
    "DESIGN_GRAPHIC_ARTS_CREATIVE": "design-or-graphic-arts-or-creative-jobs",
    "EDUCATION_TRAINING": "education-or-training-jobs",
    "ENGINEERING": "engineering-jobs",
    "FINANCE": "finance-jobs",
    "LOGISTICS_WAREHOUSE": "logistics-or-warehouse-jobs",
    "OTHER": "other-jobs",
    "PROCUREMENT_SUPPLY_CHAIN": "procurement-or-supply-chain-jobs",
    "RESEARCH_DEVELOPMENT": "research-and-development-jobs",
    "SALES_BUSINESS_DEVELOPMENT_ACCOUNT_MANAGEMENT": (
        "sales-or-business-development-or-account-management-jobs"
    ),
    "SECURITY": "security-jobs",
    "TECHNICIANS_SERVICE": "technicians-or-service-jobs",
    "WORKERS": "workers-jobs",
    "INFORMATION_TECHNOLOGY": "information-technology-jobs",
    "HEALTHCARE": "healthcare-jobs",
    "HUMAN_RESOURCES": "human-resources-jobs",
    "MARKETING": "marketing-jobs",
}


@ScraperRegistry.register(ATSType.JOBSCENTRAL_SG)
class JobsCentralScraper(BaseScraper):
    """JobsCentral SG (jobscentral.com.sg) — Singapore jobs board.

    Single-source scraper: ``company_slug`` is informational and ignored
    (pass anything — ``"any"``, ``""``, ``"sg"``).

    Knobs:
    - ``max_pages`` — pagination cap (default 50). The board is small;
      one or two pages typically cover everything.
    """

    ats = ATSType.JOBSCENTRAL_SG

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = MAX_PAGES_DEFAULT,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Probe page 0 to learn the total count and per-page limit.
            first_payload = await self._fetch_page(client, sem, page=0)
            first_jobs = first_payload.get("jobs") or {}
            count = int(first_jobs.get("count") or 0)
            limit = int(
                (first_payload.get("jobSearchModel") or {}).get("limit") or 50
            )
            await absorb(first_jobs.get("items") or [])

            if count <= limit:
                return jobs

            pages_total = (count + limit - 1) // limit
            page_count = min(pages_total, self.max_pages)
            if page_count <= 1:
                return jobs

            async def one(page: int) -> None:
                payload = await self._fetch_page(client, sem, page=page)
                items = (payload.get("jobs") or {}).get("items") or []
                await absorb(items)

            await asyncio.gather(*(one(p) for p in range(1, page_count)))
        return jobs

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        page: int,
    ) -> dict[str, Any]:
        params = {"page": page} if page else None
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        LISTING_URL, params=params, headers=_HEADERS,
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"JobsCentral fetch failed at page={page}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return _extract_page_props(response.text, page=page)
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"JobsCentral returned {response.status_code} at "
                        f"page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"JobsCentral returned {response.status_code} at page={page}"
            )
        raise ScraperError(
            f"JobsCentral exhausted retries at page={page}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        # Filter to actively-published rows. Drafts / expired entries
        # occasionally leak into the embedded list.
        status = (item.get("status") or "").upper()
        if status and status not in ("PUBLISHED", "ACTIVE"):
            return None

        company_obj = item.get("company") or {}
        company = (company_obj.get("name") or "").strip() or "Unknown"

        # Category → URL slug fallback. The router ignores the slug for
        # routing — only the trailing id matters — so misses here just
        # affect link aesthetics, not correctness.
        category = (item.get("category") or "OTHER").upper()
        slug = _CATEGORY_SLUGS.get(category) or f"{category.lower()}-jobs"
        url = f"{BASE_URL}/jobs/{slug}/{ats_id}"

        # ``location`` is a structured object; ``city`` is typically
        # "Singapore" (city-state) with optional region. ``country`` is
        # always Singapore for this domain but may be empty.
        location_obj = item.get("location") or {}
        location, country_iso = _format_location(location_obj)

        # ``remote`` is a tri-state enum: ENABLED / DISABLED / OPTIONAL.
        remote_flag = (item.get("remote") or "").upper()
        is_remote: bool | None = None
        if remote_flag == "ENABLED":
            is_remote = True
        elif remote_flag == "DISABLED":
            is_remote = False

        employment_type = _EMPLOYMENT_MAP.get(
            (item.get("occupationType") or "").upper()
        )

        description = _clean_description(item.get("shortDescription"))

        # ``tags`` is a list of {id, name} skill tags — high-signal for
        # downstream filtering, so retain in the raw overflow.
        tag_names: list[str] = []
        for t in (item.get("tags") or []):
            if isinstance(t, dict):
                name = t.get("name")
                if isinstance(name, str) and name.strip():
                    tag_names.append(name.strip())

        raw: dict[str, Any] = {}
        if tag_names:
            raw["tags"] = tag_names[:30]
        if item.get("seniority"):
            raw["seniority_level"] = item["seniority"]
        if item.get("score") is not None:
            raw["score"] = item["score"]
        if category:
            raw["category"] = category
        if company_obj.get("section"):
            raw["company_section"] = company_obj["section"]
        if item.get("requiredLanguages"):
            raw["required_languages"] = item["requiredLanguages"]

        apply_raw = (item.get("externalApplyUrl") or "").strip()
        apply_url: str | None = None
        if apply_raw.startswith(("http://", "https://")):
            apply_url = apply_raw

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.JOBSCENTRAL_SG,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Asia" if country_iso == "SG" else None,
            is_remote=is_remote,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=item.get("seniority") or None,
            apply_url=apply_url,
            description=description,
            posted_at=_parse_iso(item.get("publishedAt") or item.get("createdAt")),
            language="en",
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _extract_page_props(html: str, *, page: int) -> dict[str, Any]:
    """Pull the ``pageProps`` blob out of a ``__NEXT_DATA__`` script tag.

    Raises ``ScraperError`` when the tag is missing or unparseable —
    that's how we detect the SSR contract changing on us.
    """
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ScraperError(
            f"JobsCentral page={page} response missing __NEXT_DATA__ script"
        )
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise ScraperError(
            f"JobsCentral page={page} __NEXT_DATA__ is not valid JSON: {exc}"
        ) from exc
    props = data.get("props") or {}
    page_props = props.get("pageProps") or {}
    if not isinstance(page_props, dict):
        raise ScraperError(
            f"JobsCentral page={page} pageProps is not an object"
        )
    return page_props


def _format_location(
    location_obj: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Compose a display string from JobsCentral's structured location.

    Returns ``(location, country_iso)``. ``country_iso`` is SG when the
    country resolves to Singapore, else None — JobsCentral SG is a
    Singapore-only board but the field is occasionally empty.
    """
    if not isinstance(location_obj, dict):
        return None, None
    city = (location_obj.get("city") or "").strip()
    region = (location_obj.get("region") or "").strip()
    country = (location_obj.get("country") or "").strip()
    parts = [p for p in (city, region, country) if p]
    # Dedup adjacent duplicates (``Singapore, Singapore`` → ``Singapore``).
    cleaned: list[str] = []
    for p in parts:
        if not cleaned or cleaned[-1].lower() != p.lower():
            cleaned.append(p)
    display = ", ".join(cleaned) or None
    iso = "SG" if country.lower() == "singapore" else None
    return display, iso


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html_module.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:10_000] or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
