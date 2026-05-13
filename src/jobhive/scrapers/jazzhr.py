"""JazzHR scraper.

JazzHR ("applytojob.com") has no public JSON API — every tenant serves a
single HTML listing page at:

    GET https://{slug}.applytojob.com/apply/jobs

All open jobs are rendered in one server-side table, no pagination. Each
row looks like:

    <tr id="row_job_..." class="resumator_even_row">
      <td>
        <a class="job_title_link" href="/apply/jobs/details/{id}?&">{Title}</a>
        <br /><span class="resumator_department">{Department}</span>
      </td>
      <td>{Location}</td>
    </tr>

Detail enrichment: each job's detail page ships a clean schema.org
``JobPosting`` JSON-LD block with ``description``, ``employmentType``,
``datePosted``, structured ``jobLocation``, and ``baseSalary``
(min/max + currency + per-hour|year unit). We pull from JSON-LD to
fill description, salary range, and posted date.

Some JazzHR tenants sit behind Cloudflare and 403 plain httpx. ``client_kind``
follows the same pattern as the Eightfold scraper:

- ``"auto"`` (default): try httpx first, fall back to httpcloak on 403.
- ``"httpx"``: pinned httpx, surface 403 as an error.
- ``"httpcloak"``: skip the probe, go straight to httpcloak.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

LISTING_TEMPLATE = "https://{slug}.applytojob.com/apply/jobs"
JOB_URL_TEMPLATE = "https://{slug}.applytojob.com/apply/jobs/details/{id}"

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 8

ClientKind = Literal["auto", "httpx", "httpcloak"]

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}

# JSON-LD ``baseSalary.value.unitText`` → our ``salary_period`` enum.
_SALARY_PERIOD_MAP = {
    "HOUR": "HOUR",
    "HOURLY": "HOUR",
    "DAY": "DAY",
    "DAILY": "DAY",
    "WEEK": "WEEK",
    "WEEKLY": "WEEK",
    "MONTH": "MONTH",
    "MONTHLY": "MONTH",
    "YEAR": "YEAR",
    "YEARLY": "YEAR",
    "ANNUAL": "YEAR",
    "ANNUALLY": "YEAR",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)


# Each table row that wraps a single job. Captures everything between the
# opening <tr> and its closing </tr>.
_ROW_RE = re.compile(
    r'<tr\s+id="row_job_[^"]+"[^>]*>(?P<body>.*?)</tr>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(
    # JazzHR IDs are typically 10-char alphanumeric (e.g. `ep3PtoGGEJ`),
    # but we accept underscores/hyphens defensively in case a tenant uses
    # a non-standard scheme.
    r'<a[^>]+class="[^"]*job_title_link[^"]*"[^>]+'
    r'href="/apply/jobs/details/(?P<id>[A-Za-z0-9_-]+)[^"]*"[^>]*>'
    r'(?P<title>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_DEPT_RE = re.compile(
    r'<span[^>]*class="[^"]*resumator_department[^"]*"[^>]*>'
    r'(?P<dept>.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
# Location lives in the SECOND <td> of the row — naive: take the last <td>.
_LAST_TD_RE = re.compile(r'<td[^>]*>(?P<body>(?:(?!<td).)*?)</td>\s*$', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.JAZZHR)
class JazzHRScraper(BaseScraper):
    """JazzHR scraper — `company_slug` is the tenant subdomain."""

    ats = ATSType.JAZZHR

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        client_kind: ClientKind = "auto",
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.client_kind: ClientKind = client_kind

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
        if self.client_kind == "httpcloak":
            html_text = await asyncio.to_thread(self._fetch_via_httpcloak_sync)
            return self._parse_listing(html_text)

        # httpx or auto: try httpx first
        try:
            html_text = await self._fetch_via_httpx()
        except _WAFBlocked as exc:
            if self.client_kind == "httpx":
                raise ScraperError(
                    f"JazzHR ({self.company_slug}) blocked by WAF (403); "
                    f"set client_kind='httpcloak' to bypass"
                ) from exc
            # auto: fell back to httpcloak — skip detail enrichment too,
            # since per-job pages on a WAF-blocked tenant would 403 the
            # same way and we don't want a hidden httpx call on the
            # httpcloak path.
            html_text = await asyncio.to_thread(self._fetch_via_httpcloak_sync)
            return self._parse_listing(html_text)
        jobs = self._parse_listing(html_text)

        # Detail enrichment via JSON-LD on each job's detail page.
        # Best-effort: errors fall through silently.
        if self.include_descriptions and jobs:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
            ) as client:
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
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        _apply_jsonld_to_job(job, response.text)

    # --- httpx path -----------------------------------------------------

    async def _fetch_via_httpx(self) -> str:
        url = LISTING_TEMPLATE.format(slug=self.company_slug)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = await client.get(
                        url, headers={"User-Agent": "Mozilla/5.0"}
                    )
                except httpx.HTTPError as exc:
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"JazzHR fetch failed for {self.company_slug}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
                if response.status_code == 404:
                    raise CompanyNotFoundError(
                        f"JazzHR tenant not found: {self.company_slug}"
                    )
                if response.status_code == 403:
                    raise _WAFBlocked()
                if response.status_code == 200:
                    return response.text
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"JazzHR ({self.company_slug}) returned "
                            f"{response.status_code} after {MAX_RETRIES} retries"
                        )
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after) if retry_after and retry_after.isdigit()
                        else RETRY_BASE_DELAY * (2 ** attempt)
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ScraperError(
                    f"JazzHR ({self.company_slug}) returned {response.status_code}"
                )
        raise ScraperError(f"JazzHR ({self.company_slug}) exhausted retries")

    # --- httpcloak path -------------------------------------------------

    def _fetch_via_httpcloak_sync(self) -> str:
        try:
            import httpcloak  # noqa: F401
        except ImportError as exc:
            raise ScraperError(
                "httpcloak required for this tenant; install with "
                "`pip install httpcloak`"
            ) from exc
        return self._fetch_page_httpcloak()

    def _fetch_page_httpcloak(self) -> str:
        import httpcloak

        url = LISTING_TEMPLATE.format(slug=self.company_slug)
        try:
            response = httpcloak.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ScraperError(
                f"JazzHR ({self.company_slug}) httpcloak failed: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"JazzHR tenant not found: {self.company_slug}"
            )
        if response.status_code != 200:
            raise ScraperError(
                f"JazzHR ({self.company_slug}) httpcloak returned "
                f"{response.status_code}"
            )
        return response.text

    # --- parsing --------------------------------------------------------

    def _parse_listing(self, html_text: str) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for row_match in _ROW_RE.finditer(html_text):
            body = row_match.group("body")
            title_match = _TITLE_RE.search(body)
            if not title_match:
                continue
            ats_id = title_match.group("id")
            if ats_id in seen:
                continue
            seen.add(ats_id)
            title = _strip_tags(title_match.group("title"))
            if not title:
                continue
            dept_match = _DEPT_RE.search(body)
            department = (
                _strip_tags(dept_match.group("dept")) if dept_match else None
            ) or None
            location = self._extract_location(body)
            jobs.append(
                Job(
                    url=JOB_URL_TEMPLATE.format(slug=self.company_slug, id=ats_id),
                    title=title,
                    company=self.company_slug,
                    ats_type=ATSType.JAZZHR,
                    ats_id=ats_id,
                    location=location,
                    department=department,
                    posted_at=None,
                    fetched_at=datetime.now(),
                )
            )
        return jobs

    def _extract_location(self, row_body: str) -> str | None:
        """The location is the text content of the row's last `<td>`. We
        skip the first <td> (which contains the title link) by stripping
        anchors and department spans, then take the trailing whitespace-
        normalized text."""
        # Drop the title <td> by removing everything up through the first <br>
        # OR <span class="resumator_department">. Whichever last marker we find,
        # the remaining tail is the location <td> body.
        # Simpler: stripped text from each <td> in order; last is location.
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_body, re.DOTALL | re.IGNORECASE)
        if len(tds) < 2:
            return None
        location = _strip_tags(tds[-1])
        return location or None


class _WAFBlocked(Exception):  # noqa: N818
    """Internal signal: httpx hit a 403; caller decides whether to fall
    back to httpcloak or surface the error."""


def _apply_jsonld_to_job(job: Job, html_text: str) -> None:
    """Hydrate ``job`` from the schema.org JobPosting JSON-LD block on
    a JazzHR detail page.

    JazzHR ships a clean, standards-compliant JobPosting LD block with:
    ``description`` (HTML), ``employmentType``, ``datePosted``,
    ``jobLocation`` (Place + PostalAddress), ``baseSalary``
    (MonetaryAmount with min/max + ``unitText`` of HOUR/YEAR/etc),
    and ``uniqueJobCode`` for the canonical requisition id.

    For the ~27% of tenants on older themes that don't ship the LD
    block, we fall back to the always-present
    ``<div class="job_description">`` body for the description.
    """
    posting = _find_job_posting(html_text)
    if posting is None:
        # No JSON-LD — older theme. Description is still scrapeable
        # from the standard ``job_description`` div.
        if not job.description:
            fallback = _description_from_html(html_text)
            if fallback:
                job.description = fallback[:25_000]
        return

    if not job.description:
        desc_html = posting.get("description")
        if isinstance(desc_html, str) and desc_html.strip():
            job.description = _strip_tags(desc_html)[:25_000] or None

    emp_raw = posting.get("employmentType")
    if isinstance(emp_raw, str):
        norm = emp_raw.strip().upper().replace("-", "_").replace(" ", "_")
        mapped = _EMPLOYMENT_TYPE_MAP.get(norm)
        if mapped and not job.employment_type:
            job.employment_type = mapped

    if not job.posted_at:
        date_raw = posting.get("datePosted")
        if isinstance(date_raw, str) and date_raw:
            # JazzHR uses bare dates (``2026-04-18``); fromisoformat
            # accepts both bare and full timestamps.
            with contextlib.suppress(ValueError):
                job.posted_at = datetime.fromisoformat(
                    date_raw.replace("Z", "+00:00")
                )

    if not job.requisition_id:
        code = posting.get("uniqueJobCode")
        if isinstance(code, str) and code.strip():
            job.requisition_id = code.strip()

    if not job.location:
        loc = _location_from_jsonld(posting.get("jobLocation"))
        if loc:
            job.location = loc

    salary = _salary_from_jsonld(posting.get("baseSalary"))
    if salary:
        sal_min, sal_max, currency, period, summary = salary
        if sal_min is not None and job.salary_min is None:
            job.salary_min = sal_min
        if sal_max is not None and job.salary_max is None:
            job.salary_max = sal_max
        if currency and not job.salary_currency:
            job.salary_currency = currency
        if period and not job.salary_period:
            job.salary_period = period  # type: ignore[assignment]
        if summary and not job.salary_summary:
            job.salary_summary = summary


def _description_from_html(html_text: str) -> str | None:
    """Pull the description body out of ``<div class="job_description">``.

    Used when JSON-LD is absent. JazzHR's older theme nests the
    description text inside ``job_description_padder`` → ``job_description``;
    the inner div is what we want.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    div = soup.find("div", class_="job_description")
    if div is None:
        return None
    text = div.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _find_job_posting(html_text: str) -> dict | None:
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
    if isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from (g for g in graph if isinstance(g, dict))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_ld_dicts(item)


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


def _salary_from_jsonld(
    value: object,
) -> tuple[float | None, float | None, str | None, str | None, str | None] | None:
    """Parse ``baseSalary`` MonetaryAmount block.

    Shape: ``{"currency": "USD", "value": {"unitText": "HOUR",
    "minValue": 16, "maxValue": 20}}``. Some tenants use ``value`` as
    a flat number instead of a QuantitativeValue.
    """
    if not isinstance(value, dict):
        return None
    currency = value.get("currency")
    if isinstance(currency, str):
        currency = currency.strip().upper()
    if not isinstance(currency, str) or len(currency) > 6:
        currency = None

    inner = value.get("value")
    sal_min: float | None = None
    sal_max: float | None = None
    period: str | None = None
    if isinstance(inner, dict):
        unit = inner.get("unitText")
        if isinstance(unit, str):
            period = _SALARY_PERIOD_MAP.get(unit.strip().upper())
        for key in ("minValue", "value"):
            v = inner.get(key)
            if isinstance(v, (int, float)) and sal_min is None:
                sal_min = float(v)
        v = inner.get("maxValue")
        if isinstance(v, (int, float)):
            sal_max = float(v)
    elif isinstance(inner, (int, float)):
        sal_min = float(inner)

    if sal_max is None and sal_min is not None:
        sal_max = sal_min  # single-point salary; mirror to max for downstream consumers.

    summary = None
    if sal_min is not None or sal_max is not None:
        if sal_min == sal_max and sal_min is not None:
            summary = (
                f"{currency} {sal_min:,.0f}" if currency else f"{sal_min:,.0f}"
            )
        else:
            base = (
                f"{currency} {sal_min:,.0f}–{sal_max:,.0f}"
                if currency
                else f"{sal_min:,.0f}–{sal_max:,.0f}"
            )
            summary = f"{base} / {period.lower()}" if period else base

    if sal_min is None and currency is None:
        return None
    return sal_min, sal_max, currency, period, summary


def _strip_tags(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
