"""SEEK / JobsDB / JobStreet — APAC job board family scraper.

SEEK Ltd. owns a family of regional job boards across the Asia-Pacific
that all share the same backend search service (``chalice-search v5``).
A single scraper covers every brand because the API shape is identical
— only the host and ``siteKey`` parameter change:

    GET https://{host}/api/jobsearch/v5/search
        ?siteKey={siteKey}&page=N&pageSize=100&sortmode=ListedDate

No authentication, no API key, JSON response. The eight covered sites
host roughly **474k live postings** in aggregate (as of May 2026):

==========  =================  ===========  =======  ============
Region      Host               siteKey      ISO      Approx. jobs
==========  =================  ===========  =======  ============
au          au.seek.com        AU-Main      AU        159k
nz          www.seek.co.nz     NZ-Main      NZ         19k
hk          hk.jobsdb.com      HK-Main      HK         34k
th          th.jobsdb.com      TH-Main      TH         18k
my          my.jobstreet.com   MY-Main      MY         50k
id          id.jobstreet.com   ID-Main      ID         56k
ph          ph.jobstreet.com   PH-Main      PH         73k
sg          sg.jobstreet.com   SG-Main      SG         66k
==========  =================  ===========  =======  ============

Usage. The ``company_slug`` constructor argument picks the region:

    SeekScraper("au")   # SEEK AU only
    SeekScraper("sg")   # JobStreet Singapore only
    SeekScraper("all")  # all eight regions, sequentially

Pagination. The API caps ``pageSize`` at 100 (200 returns HTML).
``page * pageSize`` does **not** appear to have a hard cap the way
Bundesagentur / EURES do, so we iterate ``page=1..N`` until either
``len(data) < pageSize``, ``page * pageSize >= totalCount``, or a
safety limit of 1000 pages — whichever comes first.

Description handling. The search response only carries a ``teaser``
plus a few ``bulletPoints`` — the full job description requires a
per-posting fetch. With 159k AU postings alone, opting into a second
round-trip per row would be 159k extra requests; we deliberately keep
the search payload as the description source (summary only) and let
downstream enrichment fetch full bodies on demand if a consumer needs
them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)


# Region code → (host, siteKey, country_iso). The "all" sentinel is
# handled separately in ``_fetch_async`` — it isn't a real region.
SEEK_SITES: dict[str, tuple[str, str, str]] = {
    "au": ("au.seek.com", "AU-Main", "AU"),
    "nz": ("www.seek.co.nz", "NZ-Main", "NZ"),
    "hk": ("hk.jobsdb.com", "HK-Main", "HK"),
    "th": ("th.jobsdb.com", "TH-Main", "TH"),
    "my": ("my.jobstreet.com", "MY-Main", "MY"),
    "id": ("id.jobstreet.com", "ID-Main", "ID"),
    "ph": ("ph.jobstreet.com", "PH-Main", "PH"),
    "sg": ("sg.jobstreet.com", "SG-Main", "SG"),
}

# ISO 3166-1 alpha-2 → ISO 4217 currency. Only used when a row already
# has a populated ``salaryLabel`` — we never invent a currency for
# salary-less rows.
_REGION_CURRENCY: dict[str, str] = {
    "AU": "AUD",
    "NZ": "NZD",
    "HK": "HKD",
    "TH": "THB",
    "MY": "MYR",
    "ID": "IDR",
    "PH": "PHP",
    "SG": "SGD",
}

PAGE_SIZE = 100  # API caps pageSize at 100 (>100 returns HTML).
PAGE_SAFETY_CAP = 1000  # Stop after this many pages regardless of total.
MAX_CONCURRENCY = 4
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.5

# Raw overflow is capped to ~5kB serialized per the Job schema. We keep
# a small whitelist of stable identifier fields that downstream consumers
# (cross-ATS dedup, ad-tracking) may want. Heavy fields like
# ``solMetadata`` and ``tracking`` are dropped.
_RAW_KEYS = ("advertiserId", "employerId", "roleId", "displayType",
             "isFeatured", "workTypes", "workArrangements",
             "companyProfileStructuredDataId")


@ScraperRegistry.register(ATSType.SEEK)
class SeekScraper(BaseScraper):
    """SEEK / JobsDB / JobStreet — single scraper for the whole family.

    ``company_slug`` is interpreted as a region code (``au``, ``nz``,
    ``hk``, ``th``, ``my``, ``id``, ``ph``, ``sg``) or the special
    ``all`` value that iterates every region.
    """

    ats = ATSType.SEEK

    def __init__(self, company_slug: str, *, timeout: float = 30.0) -> None:
        super().__init__(company_slug, timeout=timeout)
        region = company_slug.strip().lower()
        if region == "all":
            self.regions: tuple[str, ...] = tuple(SEEK_SITES)
        elif region in SEEK_SITES:
            self.regions = (region,)
        else:
            raise ScraperError(
                f"Unknown SEEK region {company_slug!r}. "
                f"Valid: {sorted(SEEK_SITES)} or 'all'."
            )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)
            for region in self.regions:
                region_jobs = await self._fetch_region(client, sem, region)
                # Cross-region dedup — same posting can theoretically
                # appear under two siteKeys (rare but observed for
                # multi-country remote roles).
                for job in region_jobs:
                    if job.global_id in seen:
                        continue
                    seen.add(job.global_id)
                    jobs.append(job)
        return jobs

    async def _fetch_region(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        region: str,
    ) -> list[Job]:
        host, site_key, country_iso = SEEK_SITES[region]
        first = await self._search(client, sem, host=host,
                                   site_key=site_key, page=1)
        total = int(first.get("totalCount") or 0)
        data = first.get("data") or []
        jobs: list[Job] = [
            self._parse_job(item, host=host, country_iso=country_iso)
            for item in data
        ]
        if total <= PAGE_SIZE or not data:
            return [j for j in jobs if j is not None]

        # Pages beyond the first — fan out with bounded concurrency.
        last_page = min(
            (total + PAGE_SIZE - 1) // PAGE_SIZE,
            PAGE_SAFETY_CAP,
        )
        if last_page <= 1:
            return [j for j in jobs if j is not None]

        results: list[list[Job]] = [jobs]

        async def one_page(page: int) -> list[Job]:
            payload = await self._search(client, sem, host=host,
                                         site_key=site_key, page=page)
            items = payload.get("data") or []
            return [
                self._parse_job(it, host=host, country_iso=country_iso)
                for it in items
            ]

        gathered = await asyncio.gather(
            *(one_page(p) for p in range(2, last_page + 1)),
            return_exceptions=True,
        )
        for r in gathered:
            if isinstance(r, BaseException):
                log.warning("SEEK region=%s page fetch failed: %s", region, r)
                continue
            results.append(r)

        flat: list[Job] = []
        for batch in results:
            for j in batch:
                if j is not None:
                    flat.append(j)
        return flat

    async def _search(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        host: str,
        site_key: str,
        page: int,
    ) -> dict[str, Any]:
        url = (
            f"https://{host}/api/jobsearch/v5/search"
            f"?siteKey={site_key}&page={page}&pageSize={PAGE_SIZE}"
            f"&sortmode=ListedDate"
        )
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    r = await client.get(
                        url,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"SEEK fetch failed for {url}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"SEEK returned non-JSON for {url}: {exc}"
                    ) from exc
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"SEEK returned {r.status_code} for {url} after "
                        f"{MAX_RETRIES} retries"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            # 4xx other than 429 — treat as a terminal slice failure so
            # we don't burn retries on a permanent error.
            raise ScraperError(
                f"SEEK returned {r.status_code} for {url}: {r.text[:120]}"
            )
        raise ScraperError(f"SEEK exhausted retries for {url}: {last_exc}")

    def _parse_job(
        self,
        item: dict[str, Any],
        *,
        host: str,
        country_iso: str,
    ) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        advertiser = item.get("advertiser") or {}
        company = (
            advertiser.get("description")
            or item.get("companyName")
            or "Unknown"
        )

        # Location — the search response gives a list. ``label`` is the
        # display string (e.g. ``"Tampines North, East Region"``);
        # ``countryCode`` is the ISO-2. If multiple locations are listed
        # we keep the first and append the country only when the label
        # doesn't already include it (most APAC labels are city / region
        # only, no country suffix).
        locations = item.get("locations") or []
        location: str | None = None
        loc_country: str | None = None
        if locations and isinstance(locations[0], dict):
            primary = locations[0]
            label = (primary.get("label") or "").strip() or None
            loc_country = (primary.get("countryCode") or "").strip().upper() or None
            location = label

        country = loc_country or country_iso

        # Description — search response is summary-only. We concatenate
        # the teaser with the bullet points (when both are present) so
        # downstream consumers have *something* searchable; the canonical
        # 10kB cap from the model docs is preserved.
        teaser = (item.get("teaser") or "").strip()
        bullets = [
            b.strip() for b in (item.get("bulletPoints") or [])
            if isinstance(b, str) and b.strip()
        ]
        if teaser and bullets:
            description = teaser + "\n\n- " + "\n- ".join(bullets)
        elif bullets:
            description = "- " + "\n- ".join(bullets)
        else:
            description = teaser or None
        if description is not None and len(description) > 10_000:
            description = description[:10_000]

        # Classification — ``classifications[0].classification.description``
        # is the high-level category (e.g. "Information & Communication
        # Technology"). ``subclassification`` is finer-grained — we keep
        # the parent in ``department``.
        department: str | None = None
        classifications = item.get("classifications") or []
        if classifications and isinstance(classifications[0], dict):
            cls = classifications[0].get("classification") or {}
            label = (cls.get("description") or cls.get("label") or "").strip()
            department = label or None

        salary_label = (item.get("salaryLabel") or "").strip() or None
        salary_currency = _REGION_CURRENCY.get(country) if salary_label else None

        posted_at = _parse_iso8601(item.get("listingDate"))

        raw: dict[str, Any] = {}
        # Surface a compact subset of identifiers. ``advertiser.id`` is
        # the most useful cross-posting key.
        adv_id = advertiser.get("id")
        if adv_id:
            raw["advertiserId"] = str(adv_id)
        employer = item.get("employer") or {}
        emp_id = employer.get("id")
        if emp_id:
            raw["employerId"] = str(emp_id)
        for k in ("roleId", "displayType", "isFeatured", "workTypes",
                  "companyProfileStructuredDataId"):
            v = item.get(k)
            if v not in (None, "", []):
                raw[k] = v
        wa = item.get("workArrangements") or {}
        if isinstance(wa, dict) and wa.get("data"):
            # workArrangements.data is a small list of {id, label}; we
            # keep the labels as a flat list of strings.
            labels: list[str] = []
            for entry in wa["data"]:
                if not isinstance(entry, dict):
                    continue
                lbl = entry.get("label")
                if isinstance(lbl, dict):
                    text = lbl.get("text")
                    if isinstance(text, str) and text:
                        labels.append(text)
            if labels:
                raw["workArrangements"] = labels

        return Job(
            url=f"https://{host}/job/{ats_id}",
            title=title,
            company=company,
            ats_type=ATSType.SEEK,
            ats_id=ats_id,
            location=location,
            country_iso=country,
            region="Oceania" if country in ("AU", "NZ") else "Asia",
            salary_currency=salary_currency,
            salary_period="YEAR" if salary_currency else None,
            salary_summary=salary_label,
            department=department,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            language="en",
            raw=raw or None,
        )


def _parse_iso8601(value: object) -> datetime | None:
    """Parse SEEK's ``listingDate`` (``2026-04-29T00:23:37Z``).

    Returns ``None`` for missing / malformed values rather than raising —
    a single bad date in a 158k payload shouldn't abort the whole scrape.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # ``datetime.fromisoformat`` accepts a trailing offset but not the
    # literal ``Z`` shorthand until Python 3.11; we normalize defensively.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
