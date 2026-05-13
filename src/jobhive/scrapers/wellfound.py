"""Wellfound (formerly AngelList Talent) — US startup-direct jobs.

Wellfound is the largest direct-posting startup-jobs platform in the
US (~30-50k active postings). Companies pay to list, not aggregator
content.

The site sits behind Akamai and 403s every direct or proxied HTTP
GET. Scraping requires a rendering backend that runs a real browser
and bypasses the JS challenge. We use **Firecrawl** (already wired
into the library for Built In's opt-in enrichment) for the same
reason: cheap per-page, returns rendered markdown.

Library default: **no Firecrawl key, scraper raises ScraperError
with a clear configuration hint.** Pass ``firecrawl_api_key=…`` to
the constructor or set ``FIRECRAWL_API_KEY`` env to enable.

Pagination strategy: Wellfound's ``/jobs`` URL returns ~50 jobs and
isn't paginated (``?page=2`` returns the same set). To get full
coverage we walk a fixed list of role-specific URLs
(``/role/{role}``); each yields a different ~40-job slice. Dedup on
the per-job URL collapses overlap to ~1,000-2,000 unique jobs.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

WELLFOUND_BASE = "https://wellfound.com"
FIRECRAWL_BASE = "https://api.firecrawl.dev"
MAX_CONCURRENCY = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0

# Wellfound role slugs we want to enumerate. The platform exposes
# ``/role/{slug}`` for each. The list is intentionally biased toward
# tech / product / growth — Wellfound's bread-and-butter — and skews
# US since that's what Wellfound covers best.
DEFAULT_ROLE_SLUGS: tuple[str, ...] = (
    "software-engineer",
    "frontend-engineer",
    "backend-engineer",
    "fullstack-engineer",
    "mobile-engineer",
    "data-engineer",
    "data-scientist",
    "machine-learning-engineer",
    "devops-engineer",
    "engineering-manager",
    "founding-engineer",
    "product-manager",
    "designer",
    "ux-designer",
    "product-designer",
    "marketing-manager",
    "growth-marketing-manager",
    "content-marketing-manager",
    "account-executive",
    "customer-success-manager",
    "operations-manager",
    "finance-manager",
    "founders-associate",
)

# Markdown shape of a Wellfound job card (one per posting):
#   [TITLE](https://wellfound.com/jobs/{id}-{slug})
#   COMPANY • [REMOTE_FLAG] • LOCATION • $SALARY • POSTED
#
# We lean on the title-link line as the primary anchor and read the
# meta line that immediately follows for company/location/salary.
# Each Wellfound role page is grouped by company. A company block
# starts with a bold link to the company page:
#     [**Company Name**](https://wellfound.com/company/{slug})
# All job postings that follow (until the next company block or EOF)
# belong to that company.
_COMPANY_HEADER_RE = re.compile(
    r"\[\*\*([^*\n]{1,200})\*\*\]\((https?://wellfound\.com/company/[^)]+)\)"
)
# A job link: [Title](https://wellfound.com/jobs/{id}-{slug})
_TITLE_RE = re.compile(
    r"\[([^\]\n]{2,200})\]\(https?://wellfound\.com/jobs/(\d+)-([a-z0-9-]+)\)"
)
_SALARY_RANGE_RE = re.compile(
    r"\$(\d+(?:\.\d+)?)\s*([Kk]?)\s*[–\-]\s*\$?(\d+(?:\.\d+)?)\s*([Kk]?)"
)
_SALARY_SINGLE_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*([Kk]?)")
# Posted-date is on its own line: 'today', 'yesterday', '3 days ago',
# '2 months ago', etc.
_RELATIVE_RE = re.compile(
    r"^\s*(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago\s*$", re.IGNORECASE,
)
_TODAY_RE = re.compile(r"^\s*(today|yesterday|just posted)\s*$", re.IGNORECASE)
_EXPERIENCE_RE = re.compile(r"^\s*(\d+)\s*years?\s*of\s*exp\b", re.IGNORECASE)
# Location-side flag: 'Remote • United States' / 'Remote only • United States'
_REMOTE_PREFIX_RE = re.compile(r"^\s*(?:remote(?:\s+only)?|fully\s+remote)\b\s*", re.IGNORECASE)


@ScraperRegistry.register(ATSType.WELLFOUND)
class WellfoundScraper(BaseScraper):
    """Wellfound (wellfound.com) — US startup-direct jobs.

    Single-source: ``company_slug`` is ignored.

    **Firecrawl is required.** The site 403s every direct fetch
    (Akamai). Pass ``firecrawl_api_key=…`` (or set
    ``FIRECRAWL_API_KEY`` env) to enable; otherwise the scraper
    raises a clear ScraperError. Firecrawl is paid; expect roughly
    1 request per role (default ~23 roles → ~$0.02 per full run).

    Knobs:
    - ``role_slugs`` — override the role list. Default is a curated
      tech/product/growth set; pass an empty tuple to disable.
    """

    ats = ATSType.WELLFOUND

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 120.0,  # Firecrawl can take ~30-60s per page.
        firecrawl_api_key: str | None = None,
        role_slugs: tuple[str, ...] | list[str] = DEFAULT_ROLE_SLUGS,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.firecrawl_api_key = (
            firecrawl_api_key or os.environ.get("FIRECRAWL_API_KEY") or None
        )
        self.role_slugs = tuple(role_slugs)

    def fetch(self) -> list[Job]:
        if not self.firecrawl_api_key:
            raise ScraperError(
                "Wellfound requires a Firecrawl API key — the site is gated "
                "behind Akamai and won't respond to direct httpx requests. "
                "Pass firecrawl_api_key=… to the scraper or set the "
                "FIRECRAWL_API_KEY env variable."
            )
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async def absorb(items: list[Job]) -> None:
            async with lock:
                for j in items:
                    if j.ats_id in seen:
                        continue
                    seen.add(j.ats_id)
                    jobs.append(j)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_role(slug: str) -> None:
                page_jobs = await self._fetch_role(client, sem, slug)
                await absorb(page_jobs)

            # Always include the bare ``/jobs`` URL — gives ~50
            # newest-overall jobs that may not surface in any specific
            # role page yet.
            async def fetch_overall() -> None:
                page_jobs = await self._fetch_url(client, sem, f"{WELLFOUND_BASE}/jobs")
                await absorb(page_jobs)

            tasks = [fetch_overall()] + [per_role(s) for s in self.role_slugs]
            await asyncio.gather(*tasks)
            if jobs:
                await asyncio.gather(*(
                    self._enrich_description(client, sem, job) for job in jobs
                ))
        return jobs

    async def _enrich_description(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        try:
            markdown = await self._firecrawl_scrape(client, sem, str(job.url))
        except ScraperError:
            return
        if not markdown:
            return
        description = _description_from_markdown(markdown, title=job.title)
        if description and not job.description:
            job.description = description[:10_000]

    async def _fetch_role(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        slug: str,
    ) -> list[Job]:
        return await self._fetch_url(client, sem, f"{WELLFOUND_BASE}/role/{slug}")

    async def _fetch_url(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> list[Job]:
        """Render ``url`` via Firecrawl and parse its markdown for jobs."""
        markdown = await self._firecrawl_scrape(client, sem, url)
        if not markdown:
            return []
        return list(_parse_markdown(markdown))

    async def _firecrawl_scrape(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        """Single Firecrawl ``/v1/scrape`` call returning rendered
        markdown. Soft-fails (returns ``""``) on any error so a single
        bad role doesn't sink the whole run."""
        body = {"url": url, "formats": ["markdown"]}
        headers = {
            "Authorization": f"Bearer {self.firecrawl_api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.post(
                        f"{FIRECRAWL_BASE}/v1/scrape",
                        json=body, headers=headers,
                    )
                except httpx.HTTPError:
                    if attempt == MAX_RETRIES:
                        return ""
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    return ""
                return (payload.get("data") or {}).get("markdown") or ""
            if response.status_code in (408, 429) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    return ""
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            # Other status (401, 402, 403): permanent failure. Surface
            # as a hard error so the user knows their key is invalid /
            # quota'd, rather than silently returning [] for the whole
            # board.
            raise ScraperError(
                f"Firecrawl returned {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )
        return ""


# --- markdown parser --------------------------------------------------------


def _parse_markdown(md: str):
    """Yield ``Job`` instances by walking the rendered markdown.

    Wellfound's role pages are grouped by company:
      [**Company**](company-url)
      ... (description, badges)
      [Job Title](job-url)  Full-time
      $salary
      Location
      posted-date

    We walk the markdown once, tracking the most recently seen
    company-header link. Each job link inherits that company. Within
    a job's window (up to the next job link or company header), the
    field-per-line structure is parsed positionally — not all fields
    are always present, so we sniff each line and assign by shape.
    """
    seen_ids: set[str] = set()
    # Build an interleaved list of (kind, position, payload) markers
    # where kind ∈ {'company', 'job'}, sorted by position. That way
    # a single forward walk associates jobs with their preceding
    # company header.
    markers: list[tuple[int, str, re.Match]] = []
    for m in _COMPANY_HEADER_RE.finditer(md):
        markers.append((m.start(), "company", m))
    for m in _TITLE_RE.finditer(md):
        markers.append((m.start(), "job", m))
    markers.sort(key=lambda t: t[0])

    current_company: str = "Unknown"
    for i, (_pos, kind, mm) in enumerate(markers):
        if kind == "company":
            current_company = mm.group(1).strip() or "Unknown"
            continue
        # kind == "job"
        ats_id = mm.group(2)
        if ats_id in seen_ids:
            continue
        seen_ids.add(ats_id)
        title = mm.group(1).strip()
        slug = mm.group(3)
        url = f"{WELLFOUND_BASE}/jobs/{ats_id}-{slug}"

        # Window for this job's metadata: from end of this match to
        # start of the next marker (job or company), clamped so we
        # don't scan unbounded text.
        window_start = mm.end()
        window_end = markers[i + 1][0] if i + 1 < len(markers) else min(
            len(md), window_start + 800,
        )
        window = md[window_start:window_end]

        location, is_remote, salary_min, salary_max, posted, experience = (
            _parse_job_window(window)
        )

        yield Job(
            url=url,
            title=title,
            company=current_company,
            ats_type=ATSType.WELLFOUND,
            ats_id=ats_id,
            location=location,
            is_remote=is_remote,
            salary_currency="USD" if (salary_min or salary_max) else None,
            salary_period="YEAR" if (salary_min or salary_max) else None,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=experience,
            posted_at=posted,
            fetched_at=datetime.now(),
        )


def _description_from_markdown(md: str, *, title: str) -> str | None:
    """Extract a useful body from a rendered Wellfound job page.

    Role/list pages are company-grouped and include multiple card links; those
    are intentionally ignored so we don't store listing chrome as a posting
    description.
    """
    if _COMPANY_HEADER_RE.search(md):
        return None
    lines: list[str] = []
    skip_until_body = bool(title)
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if skip_until_body:
            if title.lower() in line.lower():
                skip_until_body = False
            continue
        if line in {"Apply", "Save", "SaveApply"}:
            continue
        if line.startswith("[") and "wellfound.com/jobs/" in line:
            continue
        if _parse_relative(line) is not None:
            continue
        if "$" in line and _parse_salary(line) is not None:
            continue
        cleaned = _markdown_to_text(line)
        if cleaned:
            lines.append(cleaned)
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def _markdown_to_text(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_`#>]+", "", value)
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _parse_job_window(window: str) -> tuple[
    str | None, bool | None, float | None, float | None, datetime | None, int | None,
]:
    """Walk the metadata lines after a job title link and assign each
    line to the right field by shape:
    - lines containing ``$`` → salary
    - 'today' / 'yesterday' / 'N days ago' → posted_at
    - 'N years of exp' → experience
    - 'Remote …' / 'Remote only' → is_remote (the rest is location)
    - everything else (and not a UI element) → location
    """
    location: str | None = None
    is_remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    posted: datetime | None = None
    experience: int | None = None

    for raw in window.splitlines():
        line = raw.strip()
        if not line:
            continue
        # UI/meta elements we want to ignore outright.
        if line in {"SaveApply", "Save", "Apply"}:
            continue
        if line.endswith("Save"):
            # Wellfound concatenates the date with 'Save' on a sibling
            # line ('todaySave', 'yesterdaySave'); we already captured
            # the date on the previous iteration, so skip.
            continue
        if line in {"Full-time", "Part-time", "Contract", "Internship"}:
            # Job-type chip; we surface that via the dedicated field
            # if needed, but it's not the location/salary/etc.
            continue

        # Salary
        if "$" in line and salary_min is None:
            sal = _parse_salary(line)
            if sal is not None:
                salary_min, salary_max = sal
                continue

        # Posted date
        if posted is None:
            d = _parse_relative(line)
            if d is not None:
                posted = d
                continue

        # Experience
        if experience is None:
            em = _EXPERIENCE_RE.match(line)
            if em:
                experience = int(em.group(1))
                continue

        # Remote-prefix line ('Remote • United States', 'Remote only')
        rm = _REMOTE_PREFIX_RE.match(line)
        if rm:
            is_remote = True
            tail = line[rm.end():].lstrip(" •")
            if tail and location is None:
                location = tail
            elif location is None:
                location = "Remote"
            continue

        # 'In office' / 'Onsite'
        if line.lower() in {"in office", "on-site", "onsite", "in-office"}:
            is_remote = False
            continue

        # Default: a plain location label if we don't have one yet.
        if location is None and not line.startswith("[") and len(line) < 120:
            location = line

    return location, is_remote, salary_min, salary_max, posted, experience


def _parse_salary(s: str) -> tuple[float | None, float | None] | None:
    """``$75k – $125k`` → (75000, 125000). ``$120k`` → (120000, 120000).
    Returns None if no $ amounts found."""
    m = _SALARY_RANGE_RE.search(s)
    if m:
        lo = _scale(m.group(1), m.group(2))
        hi = _scale(m.group(3), m.group(4) or m.group(2))
        return lo, hi
    m = _SALARY_SINGLE_RE.search(s)
    if m and "$" in s:
        amt = _scale(m.group(1), m.group(2))
        return amt, amt
    return None


def _scale(num: str, suffix: str) -> float:
    v = float(num)
    if suffix and suffix.lower() == "k":
        return v * 1_000
    return v


def _parse_relative(s: str) -> datetime | None:
    if _TODAY_RE.search(s):
        return datetime.now()
    m = _RELATIVE_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    seconds_per = {
        "minute": 60, "hour": 3600, "day": 86_400,
        "week": 86_400 * 7, "month": 86_400 * 30, "year": 86_400 * 365,
    }
    delta = n * seconds_per.get(unit, 0)
    if delta == 0:
        return None
    return datetime.now() - timedelta(seconds=delta)
