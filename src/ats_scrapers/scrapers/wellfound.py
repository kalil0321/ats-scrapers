"""Wellfound public role listings via a locally executed browser.

The default backend reads structured data embedded in public role pages,
including descriptions, employer references and advertised pagination.
ATS imports and automated posts are excluded. Coverage is limited to the
configured roles, not a claim to cover every Wellfound job. Blocked browser
sessions and incomplete catalogues raise rather than silently truncating.

Firecrawl remains a legacy opt-in: pass an explicit constructor API key or
``backend="firecrawl"``. An environment key alone never selects paid requests.
The legacy markdown backend cannot distinguish ATS imports or prove complete
pagination and is not the recommended production path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers import _cloakbrowser as cb
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

WELLFOUND_BASE = "https://wellfound.com"
FIRECRAWL_BASE = "https://api.firecrawl.dev"
MAX_CONCURRENCY = 4
_EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "full-time": "FULL_TIME", "part-time": "PART_TIME",
    "contract": "CONTRACT", "internship": "INTERN",
}

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

    The default requires the local ``cloakbrowser`` dependency, not an API
    key. Network-level challenges can still block datacenter hosts; browser
    rendering is not a guarantee of access. No paid fallback runs implicitly.

    Knobs:
    - ``role_slugs`` — override the role list. Default is a curated
      tech/product/growth set; pass an empty tuple to disable.
    """

    ats = ATSType.WELLFOUND

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 120.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        firecrawl_api_key: str | None = None,
        role_slugs: tuple[str, ...] | list[str] = DEFAULT_ROLE_SLUGS,
        backend: str | None = None,
        max_pages: int = 500,
        request_delay: float = 2.0,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.firecrawl_api_key = (
            firecrawl_api_key or os.environ.get("FIRECRAWL_API_KEY") or None
        )
        self.role_slugs = tuple(role_slugs)
        self.backend = backend or ("firecrawl" if firecrawl_api_key else "browser")
        if self.backend not in {"browser", "firecrawl"}:
            raise ValueError("Wellfound backend must be browser or firecrawl")
        if max_pages < 1 or request_delay < 0:
            raise ValueError("Wellfound requires positive max_pages and nonnegative request_delay")
        if any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", role) for role in self.role_slugs):
            raise ValueError("Wellfound role slugs must be lowercase URL slugs")
        self.max_pages = max_pages
        self.request_delay = request_delay

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        if self.backend != "firecrawl" or not self.firecrawl_api_key:
            return None
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(fetch, sem, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        if self.backend == "browser":
            return await self._fetch_browser()
        if not self.firecrawl_api_key:
            raise ScraperError(
                "The legacy Wellfound Firecrawl backend requires an API key. "
                "Pass firecrawl_api_key=… to the scraper or set the "
                "FIRECRAWL_API_KEY env variable."
            )
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

        async with self.make_fetcher() as fetch:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def per_role(slug: str) -> None:
                page_jobs = await self._fetch_role(fetch, sem, slug)
                await absorb(page_jobs)

            # Always include the bare ``/jobs`` URL — gives ~50
            # newest-overall jobs that may not surface in any specific
            # role page yet.
            async def fetch_overall() -> None:
                page_jobs = await self._fetch_url(fetch, sem, f"{WELLFOUND_BASE}/jobs")
                await absorb(page_jobs)

            tasks = [fetch_overall()] + [per_role(s) for s in self.role_slugs]
            await asyncio.gather(*tasks)
            if self.include_descriptions and jobs:
                await asyncio.gather(*(
                    self._enrich_description(fetch, sem, job) for job in jobs
                ))
        return jobs

    async def _fetch_browser(self) -> list[Job]:
        if not self.role_slugs:
            return []
        cb.require_cloakbrowser()
        launch_async = import_module("cloakbrowser").launch_async

        jobs: dict[str, Job] = {}
        browser = await launch_async(headless=True, humanize=False, proxy=self.proxy)
        try:
            page = await browser.new_page()
            for role in self.role_slugs:
                seen: set[str] = set()
                total = None
                page_count = None
                for number in range(1, self.max_pages + 1):
                    await asyncio.sleep(self.request_delay)
                    url = f"{WELLFOUND_BASE}/role/{role}?page={number}"
                    try:
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000),
                        )
                        if response is None or response.status != 200:
                            status = response.status if response else "no response"
                            raise ScraperError(f"Wellfound public page returned {status}: {url}")
                        await page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=10000)
                        text = await page.content()
                    except Exception as error:
                        raise ScraperError(f"Wellfound browser fetch failed: {url}: {error}") from error
                    parsed, raw_ids, pages, advertised = self._parse_browser_page(text, role, number)
                    if total is not None and (total != advertised or page_count != pages):
                        raise ScraperError(f"Wellfound catalogue changed while paging {role}")
                    total, page_count = advertised, pages
                    if raw_ids and not raw_ids - seen:
                        raise ScraperError(f"Wellfound repeated a page for {role}")
                    seen.update(raw_ids)
                    jobs.update((job.ats_id or "", job) for job in parsed)
                    if number == pages:
                        if len(seen) != advertised:
                            raise ScraperError(
                                f"Wellfound {role} advertised {advertised} jobs but exposed "
                                f"{len(seen)} unique listings; refusing incomplete coverage"
                            )
                        break
                else:
                    raise ScraperError(f"Wellfound {role} exceeds max_pages={self.max_pages}")
        finally:
            await browser.close()
        return list(jobs.values())

    def _parse_browser_page(
        self, text: str, role: str, page: int,
    ) -> tuple[list[Job], set[str], int, int]:
        try:
            script = BeautifulSoup(text, "html.parser").select_one("script#__NEXT_DATA__")
            if script is None:
                raise ValueError("missing public Next.js payload")
            data = json.loads(script.get_text())["props"]["pageProps"]["apolloState"]["data"]
            talent = data["ROOT_QUERY"]["talent"]
            prefix = "seoLandingPageJobSearchResults("
            entries = [value for key, value in talent.items() if key.startswith(prefix)
                       and json.loads(key[len(prefix):-1]) == {"role": role, "page": page}]
            if len(entries) != 1:
                raise ValueError("requested role/page not present in payload")
            result = entries[0]
            page_count, total = result["pageCount"], result["totalJobCount"]
            if type(page_count) is not int or page_count < 1 or page > page_count:
                raise ValueError("invalid pageCount")
            if type(total) is not int or total < 0:
                raise ValueError("invalid totalJobCount")
            if not isinstance(result["startups"], list):
                raise ValueError("invalid startup list")
            jobs: list[Job] = []
            seen: set[str] = set()
            for reference in result["startups"]:
                startup = data[reference["__ref"]]
                company = startup["name"].strip()
                if not company or not isinstance(startup["highlightedJobListings"], list):
                    raise ValueError("invalid employer metadata")
                for job_ref in startup["highlightedJobListings"]:
                    item = data[job_ref["__ref"]]
                    job_id = str(item["id"])
                    if not job_id.isdigit() or not item["title"].strip():
                        raise ValueError("invalid job identity")
                    seen.add(job_id)
                    if item.get("autoPosted") is not False or "atsSource" not in item or item["atsSource"]:
                        continue
                    description = item.get("description")
                    if self.include_descriptions and not (isinstance(description, str) and description.strip()):
                        raise ValueError(f"missing description for {job_id}")
                    posted = item.get("liveStartAt")
                    locations = item.get("locationNames") or []
                    if not isinstance(locations, list) or any(not isinstance(value, str) for value in locations):
                        raise ValueError("invalid locations")
                    slug = item["slug"]
                    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
                        raise ValueError("invalid job slug")
                    salary = _parse_salary(item.get("compensation") or "")
                    jobs.append(Job(
                        url=HttpUrl(f"{WELLFOUND_BASE}/jobs/{job_id}-{slug}"), title=item["title"],
                        company=company, ats_type=ATSType.WELLFOUND, ats_id=job_id,
                        description=description[:25000] if self.include_descriptions else None,
                        location=", ".join(locations) or None,
                        is_remote=item.get("remote") if isinstance(item.get("remote"), bool) else None,
                        posted_at=datetime.fromtimestamp(posted, UTC) if type(posted) is int and posted > 0 else None,
                        salary_summary=item.get("compensation") or None,
                        salary_min=salary[0] if salary else None,
                        salary_max=salary[1] if salary else None,
                        employment_type=_EMPLOYMENT_TYPES.get(item.get("jobType")),
                        fetched_at=datetime.now(UTC),
                        raw={"company_slug": startup.get("slug"), "job_type": item.get("jobType"),
                             "auto_posted": False, "ats_source": None,
                             "compensation": item.get("compensation"),
                             "accepted_remote_locations": item.get("acceptedRemoteLocationNames")},
                    ))
            if total > 0 and not seen:
                raise ValueError("nonempty catalogue returned an empty page")
            return jobs, seen, page_count, total
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
            raise ScraperError(f"Wellfound returned an invalid public listing: {error}") from error

    async def _enrich_description(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        try:
            markdown = await self._firecrawl_scrape(fetch, sem, str(job.url))
        except ScraperError:
            return
        if not markdown:
            return
        description = _description_from_markdown(markdown, title=job.title)
        if description and not job.description:
            job.description = description[:25_000]

    async def _fetch_role(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        slug: str,
    ) -> list[Job]:
        return await self._fetch_url(fetch, sem, f"{WELLFOUND_BASE}/role/{slug}")

    async def _fetch_url(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        url: str,
    ) -> list[Job]:
        """Render ``url`` via Firecrawl and parse its markdown for jobs."""
        markdown = await self._firecrawl_scrape(fetch, sem, url)
        if not markdown:
            return []
        return list(_parse_markdown(markdown))

    async def _firecrawl_scrape(
        self,
        fetch: Fetcher,
        sem: asyncio.Semaphore,
        url: str,
    ) -> str:
        """Legacy opt-in rendered markdown; transport/schema errors are fatal."""
        body = {"url": url, "formats": ["markdown"]}
        headers = {
            "Authorization": f"Bearer {self.firecrawl_api_key}",
            "Content-Type": "application/json",
        }
        async with sem:
            response = await fetch.request(
                "POST",
                f"{FIRECRAWL_BASE}/v1/scrape",
                json=body,
                headers=headers,
                handled={401, 402, 403, 404},
            )
        if response.status_code != 200:
            # Permanent failure (bad key, quota exhausted). Surface as
            # a hard error so the user knows, rather than silently
            # returning [] for the whole board.
            raise ScraperError(
                f"Firecrawl returned {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ScraperError("Firecrawl returned invalid JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ScraperError("Firecrawl returned an invalid response shape")
        markdown = payload["data"].get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ScraperError("Firecrawl returned no rendered content")
        return markdown


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
            fetched_at=datetime.now(UTC),
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
