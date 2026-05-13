"""Oracle Taleo Business Edition (TBE) careers scraper.

Taleo Business Edition is the SMB-tier Oracle ATS — its Enterprise Edition
counterpart now lives on ``oraclecloud.com`` and is handled by ``OracleScraper``.

TBE career sites live on a sharded host pattern:

    https://{ph{c}}.tbe.taleo.net/{ph{c}NN}/ats/careers/v2/searchResults
        ?org={ORG}&cws={N}

where ``ph{c}`` (``phe``, ``phf``, ``phh``, ``phq``, etc.) is a regional shard
and ``{ph{c}NN}`` is the per-tenant instance. ``ORG`` is the company code
and ``cws`` is the career-website ID.

Job links look like:

    <h4 class="oracletaleocwsv2-head-title">
      <a href=".../viewRequisition?org=X&cws=N&rid=NNN" class="viewJobLink">
        Title
      </a>
    </h4>

The scraper accepts either a full search-results URL (most reliable) or
the bare components.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 8

# Each job is rendered as `<a class="viewJobLink" href="...rid=NN">Title</a>`.
_JOB_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*viewRequisition[^"]*\brid=(?P<rid>\d+)[^"]*)"'
    r'[^>]*class="(?:[^"]*\s)?viewJobLink(?:\s[^"]*)?"[^>]*>'
    r'(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
}


@ScraperRegistry.register(ATSType.TALEO)
class TaleoScraper(BaseScraper):
    """Taleo TBE scraper. ``company_slug`` is the full search-results URL,
    e.g. ``"https://phe.tbe.taleo.net/phe01/ats/careers/v2/searchResults?org=UH9TY5&cws=41"``.

    A bare ``ORG`` code isn't enough — the regional shard (``phe`` vs ``phh``)
    and instance number (``phe01``) and ``cws`` ID vary per tenant and there's
    no public lookup. Discover the URL once via the company's careers page.
    """

    ats = ATSType.TALEO

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
        url = self._validate_url(self.company_slug)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            html_text = await self._fetch_with_retry(client, url)
            jobs = self._parse_listing(html_text, base_url=url)
            if self.include_descriptions and jobs:
                sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(client, sem, j) for j in jobs
                ))
        return jobs

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
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        _apply_jsonld_to_job(job, response.text)

    def _validate_url(self, slug: str) -> str:
        if not slug.startswith(("http://", "https://")):
            raise ScraperError(
                f"Taleo slug must be a full URL "
                f"(https://{{phN}}.tbe.taleo.net/{{phNN}}/ats/careers/v2/searchResults?org=X&cws=N), "
                f"got {slug!r}"
            )
        if "tbe.taleo.net" not in slug:
            raise ScraperError(
                f"Taleo URL must contain `tbe.taleo.net`, got {slug!r}"
            )
        return slug.rstrip("/")

    async def _fetch_with_retry(
        self, client: httpx.AsyncClient, url: str
    ) -> str:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Taleo fetch failed for {url}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(f"Taleo career site not found: {url}")
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Taleo returned {response.status_code} for {url} "
                        f"after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Taleo returned {response.status_code} for {url}"
            )
        raise ScraperError(f"Taleo exhausted retries for {url}")

    def _parse_listing(self, html_text: str, *, base_url: str) -> list[Job]:
        company = _company_from_url(base_url)
        seen: set[str] = set()
        jobs: list[Job] = []
        for match in _JOB_LINK_RE.finditer(html_text):
            rid = match.group("rid")
            if rid in seen:
                # Each job typically renders the title link plus a redundant
                # "View" button — both have viewJobLink class. Dedup by rid.
                continue
            seen.add(rid)
            href = html.unescape(match.group("href"))
            title = _strip(match.group("title"))
            if not title:
                continue
            jobs.append(
                Job(
                    url=href,
                    title=title,
                    company=company,
                    ats_type=ATSType.TALEO,
                    ats_id=rid,
                    location=None,  # location requires per-job page fetch
                    posted_at=None,
                    fetched_at=datetime.now(),
                )
            )
        return jobs


def _apply_jsonld_to_job(job: Job, html_text: str) -> None:
    """Hydrate ``job`` from the schema.org JobPosting JSON-LD on a
    Taleo TBE detail page.

    TBE pages embed a clean ``JobPosting`` block with ``description``,
    ``employmentType``, ``datePosted``, ``jobLocation`` (Place +
    PostalAddress), and ``hiringOrganization``. We pull all four when
    present.
    """
    posting = _find_job_posting(html_text)
    if posting is None:
        return

    desc = posting.get("description")
    if isinstance(desc, str) and desc.strip() and not job.description:
        job.description = _strip_jsonld_html(desc)[:25_000] or None

    emp = posting.get("employmentType")
    if isinstance(emp, str) and not job.employment_type:
        norm = emp.strip().lower()
        for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
            if needle in norm:
                job.employment_type = mapped
                break
        if not job.commitment:
            job.commitment = emp.strip()

    if not job.posted_at:
        date_raw = posting.get("datePosted")
        if isinstance(date_raw, str) and date_raw.strip():
            cleaned = date_raw.strip().replace("Z", "+00:00")
            try:
                job.posted_at = datetime.fromisoformat(cleaned)
            except ValueError:
                # TBE often ships ``"2025-07-28 00:00:00.0"`` form.
                cleaned_no_tz = re.sub(r"\.\d+$", "", cleaned)
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        job.posted_at = datetime.strptime(cleaned_no_tz, fmt)
                        break
                    except ValueError:
                        continue

    if not job.location:
        loc = _location_from_jsonld(posting.get("jobLocation"))
        if loc:
            job.location = loc

    org = posting.get("hiringOrganization")
    if isinstance(org, dict):
        name = org.get("name")
        if isinstance(name, str) and name.strip():
            job.company = name.strip()


def _find_job_posting(html_text: str) -> dict | None:
    for match in _JSON_LD_RE.finditer(html_text):
        body = match.group(1).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
    return None


def _location_from_jsonld(value: object) -> str | None:
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


def _strip_jsonld_html(text: str) -> str:
    out = _TAG_RE.sub(" ", text)
    out = html.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def _company_from_url(url: str) -> str:
    """Extract the ``org`` query parameter as the company name. Falls back
    to the host's first label."""
    m = re.search(r"[?&]org=([^&#]+)", url)
    if m:
        return m.group(1)
    host = urlparse(url).hostname or ""
    return host.split(".", 1)[0]


def _strip(text: str) -> str:
    """Strip tags + entities + collapse whitespace."""
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
