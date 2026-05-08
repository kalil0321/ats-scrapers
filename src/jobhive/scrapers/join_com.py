"""Join.com scraper.

Two-step API: resolve slug → company_id, then fetch company jobs.

    GET https://join.com/companies/{slug}        # returns metadata with id
    GET https://join.com/api/public/companies/{id}/jobs

After the listing pass we enrich each job with the schema.org JSON-LD
JobPosting block on its detail page (description body, baseSalary,
jobLocationType). Detail fetches run in a small thread pool so a tenant
with 50 open positions still finishes in a few seconds.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://join.com"
API_BASE = f"{BASE_URL}/api/public"
DETAIL_CONCURRENCY = 8

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}

_SALARY_PERIOD_MAP = {
    "HOUR": "HOUR",
    "DAY": "DAY",
    "WEEK": "WEEK",
    "MONTH": "MONTH",
    "YEAR": "YEAR",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.JOIN_COM)
class JoinComScraper(BaseScraper):
    ats = ATSType.JOIN_COM

    def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            company_id = self._resolve_company_id(client)
            page = 1
            while True:
                params = {
                    "locale": "en-us",
                    "page": page,
                    "pageSize": 100,
                    "withAggregations": "true",
                    "sort": "+title",
                }
                try:
                    response = client.get(
                        f"{API_BASE}/companies/{company_id}/jobs", params=params
                    )
                except httpx.HTTPError as exc:
                    raise ScraperError(
                        f"join.com jobs fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                if response.status_code != 200:
                    raise ScraperError(
                        f"join.com returned {response.status_code} listing jobs for "
                        f"{self.company_slug}"
                    )
                payload = response.json()
                items = payload.get("items") or []
                all_jobs.extend(self._parse_job(item) for item in items)
                pagination = payload.get("pagination") or {}
                if page >= pagination.get("totalPages", page):
                    break
                page += 1

            if all_jobs:
                self._enrich_with_details(client, all_jobs)
        return all_jobs

    def _enrich_with_details(
        self, client: httpx.Client, jobs: list[Job],
    ) -> None:
        """Per-job detail fetch — pull JSON-LD (description, salary,
        remote flag) from each job page. Runs in a thread pool to avoid
        blocking on N+1 sequential requests.

        Each detail page is independent of the listing client, so we
        spin up short-lived per-thread clients with their own connection
        pool. ``httpx.Client`` is not thread-safe.
        """
        def fetch_one(job: Job) -> None:
            try:
                with httpx.Client(
                    timeout=self.timeout, follow_redirects=True,
                ) as detail_client:
                    response = detail_client.get(
                        str(job.url),
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
            except httpx.HTTPError:
                return
            if response.status_code != 200:
                return
            _apply_jsonld_to_job(job, response.text)

        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            list(pool.map(fetch_one, jobs))

    def _resolve_company_id(self, client: httpx.Client) -> str:
        try:
            response = client.get(f"{BASE_URL}/companies/{self.company_slug}")
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"join.com company resolve failed for {self.company_slug}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(f"join.com company not found: {self.company_slug}")
        body = response.text
        # The page embeds the same Next.js page data several times. The
        # *first* numeric ``"id"`` in the body is **not** the company id
        # — that's the first department/category in the picker (e.g.
        # ``"id":233,"name":"Administration and Secretariat"``). Anchor
        # on the explicit company object instead, and *validate* that
        # the embedded ``domain`` matches the slug we asked for. join.com
        # has been observed serving template/default pages for newly-
        # created tenants where ``"company":{"id":233,...}`` (greenteg)
        # is rendered as a placeholder before the real tenant data
        # hydrates — without the domain check we would attribute
        # greenteg's jobs to whichever slug was being scraped.
        with_domain = re.search(
            r'"company"\s*:\s*\{\s*'
            r'"id"\s*:\s*"?(?P<id>\d+)"?\s*,\s*'
            r'"name"\s*:\s*"[^"]*"\s*,\s*'
            r'"domain"\s*:\s*"(?P<domain>[^"]+)"',
            body,
        )
        if with_domain:
            resolved_id = with_domain.group("id")
            resolved_domain = with_domain.group("domain").lower()
            if resolved_domain != self.company_slug.lower():
                raise CompanyNotFoundError(
                    f"join.com slug {self.company_slug!r} resolved to a "
                    f"different tenant (domain={resolved_domain!r}, "
                    f"id={resolved_id}) — likely a placeholder/cached page"
                )
            return resolved_id
        # Fallbacks for pages that don't include the domain field. These
        # don't get a domain check, so they're a last resort.
        for pattern in (
            r'"company"\s*:\s*\{\s*"id"\s*:\s*"?(\d+)"?',
            r'"companyId"\s*:\s*"?(\d+)"?',
        ):
            match = re.search(pattern, body)
            if match:
                return match.group(1)
        raise ScraperError(
            f"join.com page for {self.company_slug} did not expose a company id"
        )

    def _parse_job(self, item: dict[str, Any]) -> Job:
        raw: dict[str, Any] = {}
        for k in ("department", "category", "industry", "skills",
                  "language", "employmentType", "remoteWork", "workplaceType"):
            v = item.get(k)
            if v:
                raw[k] = v

        # join.com's API returns ``city`` as an object — pull a flat
        # ``City, Country`` label out of it. ``employmentType`` and
        # ``department`` are similarly structured; fall through to None
        # when they aren't a plain string.
        location = _flatten_location(item.get("location"), item.get("city"))
        department = _name_or_none(item.get("department"))
        employment_type = _name_or_none(item.get("employmentType"))

        # The browser-visible URL uses the slug-style ``idParam``, not the
        # numeric id (which only the API uses). Falling back to a numeric
        # path 404s.
        slug_param = item.get("idParam") or item["id"]
        url = item.get("url") or (
            f"{BASE_URL}/companies/{self.company_slug}/jobs/{slug_param}"
        )

        return Job(
            url=url,
            title=item["title"].strip(),
            company=self.company_slug,
            ats_type=ATSType.JOIN_COM,
            ats_id=str(item["id"]),
            location=location,
            department=department,
            commitment=employment_type,
            posted_at=_parse_iso(item.get("publishedAt") or item.get("createdAt")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _apply_jsonld_to_job(job: Job, html_text: str) -> None:
    """Hydrate ``job`` from the ``JobPosting`` JSON-LD on a join.com detail page.

    join.com renders one LD block per detail page with:

    * ``description`` — HTML, double-encoded (entities like ``&lt;p&gt;``).
    * ``employmentType`` — already canonical (``FULL_TIME``, ``PART_TIME``…).
    * ``baseSalary`` — ``MonetaryAmount`` with ``min/maxValue`` + ``unitText``.
    * ``jobLocationType`` — ``"TELECOMMUTE"`` for remote roles.
    * ``directApply`` — boolean (informational, kept in raw).
    """
    posting = _find_job_posting(html_text)
    if posting is None:
        return

    if not job.description:
        desc = posting.get("description")
        if isinstance(desc, str) and desc.strip():
            job.description = _strip_double_encoded(desc)[:10_000] or None

    emp = posting.get("employmentType")
    if isinstance(emp, str) and not job.employment_type:
        norm = emp.strip().upper().replace("-", "_").replace(" ", "_")
        mapped = _EMPLOYMENT_TYPE_MAP.get(norm)
        if mapped:
            job.employment_type = mapped

    if posting.get("jobLocationType") == "TELECOMMUTE" and job.is_remote is None:
        job.is_remote = True

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


def _strip_double_encoded(text: str) -> str:
    """join.com's ``description`` is HTML with the *entities* escaped:
    ``&lt;p&gt;...`` literally. Decode once, then strip the resulting tags."""
    decoded = html_mod.unescape(text)
    decoded = _TAG_RE.sub(" ", decoded)
    decoded = html_mod.unescape(decoded)  # second pass for nested entities
    return re.sub(r"\s+", " ", decoded).strip()


def _salary_from_jsonld(
    value: object,
) -> tuple[float | None, float | None, str | None, str | None, str | None] | None:
    if not isinstance(value, dict):
        return None
    currency = value.get("currency")
    if isinstance(currency, str):
        currency = currency.strip().upper()
        if len(currency) > 6:
            currency = None
    else:
        currency = None

    inner = value.get("value")
    sal_min: float | None = None
    sal_max: float | None = None
    period: str | None = None
    if isinstance(inner, dict):
        unit = inner.get("unitText")
        if isinstance(unit, str):
            period = _SALARY_PERIOD_MAP.get(unit.strip().upper())
        v = inner.get("minValue")
        if isinstance(v, (int, float)):
            sal_min = float(v)
        v = inner.get("maxValue")
        if isinstance(v, (int, float)):
            sal_max = float(v)
        if sal_min is None:
            v = inner.get("value")
            if isinstance(v, (int, float)):
                sal_min = float(v)
    elif isinstance(inner, (int, float)):
        sal_min = float(inner)

    if sal_max is None and sal_min is not None:
        sal_max = sal_min

    if sal_min is None and currency is None:
        return None

    summary = None
    if sal_min is not None or sal_max is not None:
        if sal_min == sal_max and sal_min is not None:
            base = (
                f"{currency} {sal_min:,.0f}" if currency else f"{sal_min:,.0f}"
            )
        else:
            base = (
                f"{currency} {sal_min:,.0f}–{sal_max:,.0f}"
                if currency
                else f"{sal_min:,.0f}–{sal_max:,.0f}"
            )
        summary = f"{base} / {period.lower()}" if period else base

    return sal_min, sal_max, currency, period, summary


def _flatten_location(*values: object) -> str | None:
    """Return the first usable location label across the supplied values.

    join.com sometimes ships ``location`` as a string and sometimes only
    fills ``city`` (object: ``{"cityName": "...", "countryName": "..."}``).
    Walk both and produce a flat ``City, Country`` string."""
    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        if isinstance(value, dict):
            city = (value.get("cityName") or value.get("city") or "").strip()
            country = (value.get("countryName") or value.get("country") or "").strip()
            label = ", ".join(p for p in (city, country) if p)
            if label:
                return label
    return None


def _name_or_none(value: object) -> str | None:
    """``department``/``employmentType`` may be a string or a dict with
    ``name``; only return a non-empty string."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
