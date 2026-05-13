"""iCIMS Talent Cloud careers scraper.

Used by Disney, Kroger, AT&T, Visa, Peraton, Audacy, Vioc, and many others.

iCIMS career sites are HTML only — no public JSON. Each tenant lives on
``careers-{slug}.icims.com`` (sometimes ``uscareers-{slug}.icims.com``). The
visible careers page embeds an iframe with the actual job listings; we hit
the iframe URL directly to skip the wrapper:

    GET https://careers-{slug}.icims.com/jobs/search?ss=1&pr={page}&in_iframe=1

Each posting is wrapped in a ``<li class="iCIMS_JobCardItem">`` block:

    <li class="iCIMS_JobCardItem">
      <div class="col-xs-6 header left">
        <span class="sr-only field-label">Job Locations</span>
        <span> US-CA-Monrovia</span>          ← location lives here
      </div>
      <div class="col-xs-6 header right">
        <span title="5/6/2026 10:23 AM">3 hours ago</span>   ← posted_at
      </div>
      <div class="col-xs-12 title">
        <a href=".../jobs/{id}/{slug}/job?in_iframe=1" class="iCIMS_Anchor">
          <h3>Title</h3>
        </a>
      </div>
      <div class="col-xs-12 description">summary text...</div>
    </li>

Pagination via ``pr={N}``, 0-indexed. Each page typically holds 25 jobs.
We paginate until a page yields no new IDs.

Detail enrichment: each job's iframe URL (``/jobs/{id}/{slug}/job?in_iframe=1``)
ships a schema.org ``JobPosting`` JSON-LD with the full description,
``employmentType``, ``datePosted``, ``occupationalCategory``, and
structured ``jobLocation``. We pull from JSON-LD when the listing
truncates the summary or omits employment type / posted date.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

MAX_PAGES = 200  # Safety bound; iCIMS tenants rarely exceed 5K jobs.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 8  # per-tenant cap on detail-page fetches

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)

# Each posting is one <li class="iCIMS_JobCardItem">…</li>. We match the whole
# card so location, posted_at and description (which sit OUTSIDE the anchor)
# stay associated with the right job.
_JOB_CARD_RE = re.compile(
    r'<li[^>]+class="[^"]*iCIMS_JobCardItem[^"]*"[^>]*>(?P<body>.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)
# Anchor inside a card — gives us href, id, and the <h3> title.
_JOB_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(?P<href>https?://[^"]*?/jobs/(?P<id>\d+)/[^"]*?/job[^"]*)"[^>]*'
    r'class="[^"]*iCIMS_Anchor[^"]*"[^>]*>'
    r'(?P<inner>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(r'<h3[^>]*>(?P<title>.*?)</h3>', re.DOTALL | re.IGNORECASE)
# `<span class="sr-only field-label">Job Locations</span> <span>VALUE</span>`
# captures the visible location string (iCIMS uses the format
# "US-SC-Prosperity" — country-state-city).
_LOCATION_RE = re.compile(
    r'<span[^>]+class="[^"]*sr-only[^"]*field-label[^"]*"[^>]*>\s*Job Locations\s*</span>'
    r'\s*<span[^>]*>\s*(?P<loc>[^<]*?)\s*</span>',
    re.DOTALL | re.IGNORECASE,
)
# Posted-at: `<span title="5/6/2026 10:23 AM">3 hours ago…</span>`. The
# title attribute is the absolute timestamp; relative ("3 hours ago") is the
# label. We parse the title.
_DATE_TITLE_RE = re.compile(
    r'<span[^>]+title="(?P<date>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)"',
    re.IGNORECASE,
)
# Per-job header tags inside `iCIMS_JobHeaderGroup`. Two shapes:
#   <dt class="iCIMS_JobHeaderField">Requisition ID</dt>            ← plain
#   <dt><span class="glyphicons …"></span>                          ← icon
#       <span class="sr-only field-label">Location : City</span></dt>
# The label is whatever readable text sits inside the <dt>, with any
# leading icon/sr-only wrappers stripped — extract by removing tags.
_HEADER_TAG_RE = re.compile(
    r'<dt[^>]*>(?P<label_html>.*?)</dt>'
    r'\s*<dd[^>]*>\s*<span[^>]*>(?P<value>.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
_DESC_RE = re.compile(
    r'<div[^>]+class="[^"]*col-xs-12[^"]*description[^"]*"[^>]*>(?P<desc>.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.ICIMS)
class iCIMSScraper(BaseScraper):  # noqa: N801  matches public iCIMS branding
    """iCIMS scraper. ``company_slug`` is either:

    - A bare slug — ``"peraton"`` → ``https://careers-peraton.icims.com``
    - A full URL — ``"https://uscareers-rws.icims.com"`` (for the
      ``uscareers-`` variant or any custom subdomain)
    """

    ats = ATSType.ICIMS

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.base_url = self._resolve_base_url(company_slug)

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()

        async def run() -> str | None:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
            ) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_detail(client, sem, copy)
            return copy.description

        return asyncio.run(run())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        all_jobs: list[Job] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for page_num in range(MAX_PAGES):
                html_text = await self._fetch_page(client, page=page_num)
                page_jobs = self._parse_page(html_text)
                new = [j for j in page_jobs if j.ats_id not in seen]
                if not new:
                    break
                for j in new:
                    seen.add(j.ats_id)
                all_jobs.extend(new)

            # Detail enrichment: pull schema.org JSON-LD from each job's
            # iframe page. Best-effort — failures keep the listing-derived
            # row.
            if self.include_descriptions and all_jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(client, sem, j) for j in all_jobs
                ))
        return all_jobs

    async def _enrich_detail(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await client.get(
                    str(job.url),
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        _apply_jsonld_to_job(job, response.text)

    def _resolve_base_url(self, slug: str) -> str:
        if slug.startswith(("http://", "https://")):
            return slug.rstrip("/")
        return f"https://careers-{slug}.icims.com"

    async def _fetch_page(
        self, client: httpx.AsyncClient, *, page: int
    ) -> str:
        url = f"{self.base_url}/jobs/search"
        params = {"ss": "1", "pr": page, "in_iframe": "1"}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url,
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"iCIMS fetch failed for {self.base_url} at page={page}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(
                    f"iCIMS site not found: {self.base_url}"
                )
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"iCIMS returned {response.status_code} for "
                        f"{self.base_url} at page={page} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"iCIMS returned {response.status_code} for {self.base_url} "
                f"at page={page}"
            )
        raise ScraperError(
            f"iCIMS exhausted retries for {self.base_url} at page={page}"
        )

    def _parse_page(self, html_text: str) -> list[Job]:
        jobs: list[Job] = []
        seen_in_page: set[str] = set()
        company = self._company_name()
        for card in _JOB_CARD_RE.finditer(html_text):
            body = card.group("body")
            anchor = _JOB_ANCHOR_RE.search(body)
            if anchor is None:
                continue
            ats_id = anchor.group("id")
            if ats_id in seen_in_page:
                # iCIMS sometimes renders multiple anchors per job (title +
                # icon link); dedup within the page so cross-page logic
                # gets clean input.
                continue
            seen_in_page.add(ats_id)
            title_match = _TITLE_RE.search(anchor.group("inner"))
            if not title_match:
                continue
            title = _strip(title_match.group("title"))
            if not title:
                continue
            jobs.append(
                Job(
                    url=html.unescape(anchor.group("href")),
                    title=title,
                    company=company,
                    ats_type=ATSType.ICIMS,
                    ats_id=ats_id,
                    location=_extract_location(body),
                    posted_at=_extract_posted_at(body),
                    description=_extract_description(body),
                    requisition_id=_extract_requisition_id(body),
                    department=_extract_header_value(body, "Category"),
                    fetched_at=datetime.now(),
                )
            )
        return jobs

    def _company_name(self) -> str:
        # `careers-peraton.icims.com` → `peraton`
        # `uscareers-rws.icims.com` → `rws`
        host = self.base_url.replace("https://", "").replace("http://", "")
        host = host.split("/", 1)[0]
        if host.startswith("careers-"):
            return host.removeprefix("careers-").split(".", 1)[0]
        if host.startswith("uscareers-"):
            return host.removeprefix("uscareers-").split(".", 1)[0]
        return host.split(".", 1)[0]


def _apply_jsonld_to_job(job: Job, html_text: str) -> None:
    """Hydrate ``job`` from the schema.org JobPosting JSON-LD on iCIMS
    detail pages.

    iCIMS embeds a clean ``<script type="application/ld+json">``
    JobPosting block on every job iframe page. Fields:

    * ``description`` — full HTML body (strip + decode for plain text).
    * ``employmentType`` — already in ``FULL_TIME`` / ``PART_TIME`` form.
    * ``datePosted`` — ISO 8601.
    * ``occupationalCategory`` — the iCIMS "Category" facet.
    * ``jobLocation`` — structured ``Place`` object with addressLocality.

    Best-effort: silently skips jobs without a parseable LD block.
    """
    posting = _find_job_posting(html_text)
    if posting is None:
        return

    if not job.description:
        desc_html = posting.get("description")
        if isinstance(desc_html, str) and desc_html.strip():
            job.description = _strip(desc_html)[:25_000] or None

    emp_raw = posting.get("employmentType")
    if isinstance(emp_raw, str):
        norm = emp_raw.strip().upper().replace("-", "_").replace(" ", "_")
        mapped = _EMPLOYMENT_TYPE_MAP.get(norm)
        if mapped and not job.employment_type:
            job.employment_type = mapped

    if not job.posted_at:
        date_raw = posting.get("datePosted")
        if isinstance(date_raw, str) and date_raw:
            with contextlib.suppress(ValueError):
                job.posted_at = datetime.fromisoformat(
                    date_raw.replace("Z", "+00:00")
                )

    if not job.department:
        cat = posting.get("occupationalCategory")
        if isinstance(cat, str) and cat.strip():
            job.department = cat.strip()

    if not job.location:
        loc_str = _location_from_jsonld(posting.get("jobLocation"))
        if loc_str:
            job.location = loc_str


def _find_job_posting(html_text: str) -> dict | None:
    """Walk every JSON-LD block until one matches ``@type: JobPosting``."""
    for match in _JSON_LD_RE.finditer(html_text):
        body = match.group(1).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for candidate in _iter_ld_dicts(data):
            if candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _iter_ld_dicts(node: object):
    """JSON-LD payloads can be a single dict, a list of dicts, or a
    ``@graph`` wrapper. Yield every dict so the caller can pick the
    JobPosting one."""
    if isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from (g for g in graph if isinstance(g, dict))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_ld_dicts(item)


def _location_from_jsonld(value: object) -> str | None:
    """Schema.org ``jobLocation`` is either a Place dict or a list of them."""
    candidates = value if isinstance(value, list) else [value]
    for c in candidates:
        if not isinstance(c, dict):
            continue
        addr = c.get("address")
        if not isinstance(addr, dict):
            continue
        parts = [
            str(addr.get(k) or "").strip()
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if addr.get(k)
        ]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined
    return None


def _strip(text: str) -> str:
    """Strip tags + entities + collapse whitespace from inner HTML."""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# iCIMS encodes locations as Country-State-City (e.g. "US-SC-Prosperity",
# "USA-MD-Baltimore", "CA-ON-Toronto", "FR-Paris"). We reverse to City,
# State, Country for human readability and consistency with the other ATS
# scrapers — but only when we recognize the dash-separated shape; opaque
# strings ("Remote", "Multiple Locations") pass through unchanged.
_DASH_LOC_RE = re.compile(
    r'^(?P<country>[A-Z]{2,3})-(?P<state>[A-Z0-9 ]{1,40})(?:-(?P<city>[^-].*))?$'
)


def _extract_location(card_body: str) -> str | None:
    match = _LOCATION_RE.search(card_body)
    if match:
        raw = _strip(match.group("loc"))
        if raw:
            return _normalize_location(raw)
    # Fall back to the per-job header tags (City / State / Country).
    parts: dict[str, str] = {}
    for tag in _HEADER_TAG_RE.finditer(card_body):
        label = _strip(tag.group("label_html")).lower()
        value = _strip(tag.group("value"))
        if not value:
            continue
        if "city" in label:
            parts["city"] = value
        elif "state" in label or "province" in label:
            parts["state"] = value
        elif "country" in label:
            parts["country"] = value
    if parts:
        ordered = [parts.get(k) for k in ("city", "state", "country")]
        return ", ".join(p for p in ordered if p)
    return None


def _normalize_location(raw: str) -> str:
    match = _DASH_LOC_RE.match(raw)
    if not match:
        return raw
    parts = [match.group("city"), match.group("state"), match.group("country")]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _extract_posted_at(card_body: str) -> datetime | None:
    match = _DATE_TITLE_RE.search(card_body)
    if not match:
        return None
    raw = match.group("date").strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _extract_description(card_body: str) -> str | None:
    match = _DESC_RE.search(card_body)
    if not match:
        return None
    text = _strip(match.group("desc"))
    return text or None


def _extract_requisition_id(card_body: str) -> str | None:
    return _extract_header_value(card_body, "Requisition ID") or _extract_header_value(card_body, "ID")


def _extract_header_value(card_body: str, label_match: str) -> str | None:
    """Look up a `<dt>{label}</dt><dd>{value}</dd>` pair by exact label."""
    needle = label_match.lower()
    for tag in _HEADER_TAG_RE.finditer(card_body):
        if _strip(tag.group("label_html")).lower() == needle:
            value = _strip(tag.group("value"))
            return value or None
    return None
