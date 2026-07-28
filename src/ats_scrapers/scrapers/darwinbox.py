"""Darwinbox multi-tenant careers scraper.

Darwinbox's current candidate portal lists jobs through::

    POST https://{tenant}.darwinbox.{in,com}/ms/candidateapi/job/alljobs

The endpoint is Cloudflare-protected and requires the shared ``httpcloak``
transport. It returns ten jobs per page and includes full descriptions in the
listing payload, so no detail-page fan-out is required.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 10
MAX_PAGES = 500
_DEFAULT_TLD = "in"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TENANT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_TYPE_MAP: dict[str, EmploymentType] = {
    "permanent": "FULL_TIME",
    "employee": "FULL_TIME",
    "regular": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "parttime": "PART_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "consultant": "CONTRACT",
    "fixed term": "CONTRACT",
    "fixed-term": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "apprentice": "INTERN",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
}
_COUNTRY_METADATA = {
    "argentina": ("AR", "South America"),
    "australia": ("AU", "Oceania"),
    "austria": ("AT", "Europe"),
    "bahrain": ("BH", "Asia"),
    "bangladesh": ("BD", "Asia"),
    "belgium": ("BE", "Europe"),
    "brazil": ("BR", "South America"),
    "cambodia": ("KH", "Asia"),
    "canada": ("CA", "North America"),
    "chile": ("CL", "South America"),
    "china": ("CN", "Asia"),
    "colombia": ("CO", "South America"),
    "czech republic": ("CZ", "Europe"),
    "denmark": ("DK", "Europe"),
    "finland": ("FI", "Europe"),
    "france": ("FR", "Europe"),
    "germany": ("DE", "Europe"),
    "hong kong": ("HK", "Asia"),
    "india": ("IN", "Asia"),
    "indonesia": ("ID", "Asia"),
    "ireland": ("IE", "Europe"),
    "italy": ("IT", "Europe"),
    "japan": ("JP", "Asia"),
    "kuwait": ("KW", "Asia"),
    "malaysia": ("MY", "Asia"),
    "mexico": ("MX", "North America"),
    "myanmar": ("MM", "Asia"),
    "nepal": ("NP", "Asia"),
    "netherlands": ("NL", "Europe"),
    "new zealand": ("NZ", "Oceania"),
    "norway": ("NO", "Europe"),
    "oman": ("OM", "Asia"),
    "pakistan": ("PK", "Asia"),
    "philippines": ("PH", "Asia"),
    "poland": ("PL", "Europe"),
    "portugal": ("PT", "Europe"),
    "qatar": ("QA", "Asia"),
    "saudi arabia": ("SA", "Asia"),
    "singapore": ("SG", "Asia"),
    "south africa": ("ZA", "Africa"),
    "south korea": ("KR", "Asia"),
    "spain": ("ES", "Europe"),
    "sri lanka": ("LK", "Asia"),
    "sweden": ("SE", "Europe"),
    "switzerland": ("CH", "Europe"),
    "taiwan": ("TW", "Asia"),
    "thailand": ("TH", "Asia"),
    "united arab emirates": ("AE", "Asia"),
    "united kingdom": ("GB", "Europe"),
    "united states": ("US", "North America"),
    "united states of america": ("US", "North America"),
    "vietnam": ("VN", "Asia"),
}


@ScraperRegistry.register(ATSType.DARWINBOX)
class DarwinboxScraper(BaseScraper):
    """Scrape one Darwinbox tenant from a bare slug, slug+TLD, or URL."""

    ats = ATSType.DARWINBOX
    fetch_engine = "cloak"
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        tenant, tld = self._resolve_tenant()
        base_url = f"https://{tenant}.darwinbox.{tld}"
        api_url = f"{base_url}/ms/candidateapi/job/alljobs"
        headers = {
            **self.default_headers,
            "Origin": base_url,
            "Referer": f"{base_url}/ms/candidatev2/main/careers/allJobs",
        }

        jobs: list[Job] = []
        seen: set[str] = set()
        total: int | None = None
        async with self.make_fetcher(headers=headers) as fetch:
            for page in range(1, MAX_PAGES + 1):
                payload = await self._fetch_page(fetch, api_url, page)
                page_jobs = payload.get("data")
                if not isinstance(page_jobs, list) or not page_jobs:
                    break
                for item in page_jobs:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_listing(item, tenant, tld, base_url)
                    if job is None or not job.ats_id or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

                total = _coerce_int(payload.get("job_counts"))
                if total is not None and len(jobs) >= total:
                    break
                if len(page_jobs) < PAGE_SIZE:
                    break
            else:
                raise ScraperError(
                    "Darwinbox pagination reached the safety cap "
                    f"({MAX_PAGES} pages, {len(jobs)} of {total or 'unknown'} jobs)"
                )
        return jobs

    async def _fetch_page(
        self,
        fetch: Fetcher,
        api_url: str,
        page: int,
    ) -> dict[str, Any]:
        payload = await fetch.post_json(
            api_url,
            params={"companyId": "main"},
            json={
                "companyId": "main",
                "page": page,
                "sort_option": "new",
                "limit": PAGE_SIZE,
            },
        )
        if not isinstance(payload, dict):
            raise ScraperError(f"Darwinbox returned a non-object payload at page={page}")
        if payload.get("status") != "success":
            raise ScraperError(f"Darwinbox API failure at page={page}: {payload!r}")
        return payload

    def _resolve_tenant(self) -> tuple[str, str]:
        raw = self.company_slug.strip()
        if not raw:
            raise ScraperError("Darwinbox scraper requires a non-empty company_slug")
        if "://" in raw:
            host = (urlparse(raw).hostname or "").lower()
            match = re.fullmatch(
                rf"({_TENANT_RE.pattern})\.darwinbox\.(in|com)", host
            )
            if not match:
                raise ScraperError(
                    f"Darwinbox slug must be a darwinbox.{{in,com}} URL, got {raw!r}"
                )
            return match.group(1), match.group(2)
        if raw.endswith(".com"):
            tenant, tld = raw[:-4], "com"
        elif raw.endswith(".in"):
            tenant, tld = raw[:-3], "in"
        else:
            tenant, tld = raw, _DEFAULT_TLD
        if not _TENANT_RE.fullmatch(tenant):
            raise ScraperError(
                f"Darwinbox slug {raw!r} looks malformed; expected a DNS-safe subdomain"
            )
        return tenant, tld

    def _parse_listing(
        self,
        item: dict[str, Any],
        tenant: str,
        tld: str,
        base_url: str,
    ) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = _first_string(
            item.get("designation_display_name"),
            item.get("title"),
            item.get("designation_name"),
        )
        if not ats_id or not title:
            return None

        commitment = _first_string(
            item.get("emp_type_name"),
            item.get("emp_type"),
        )
        country = _coerce_string(item.get("country"))
        country_metadata = _COUNTRY_METADATA.get(country.casefold()) if country else None
        country_iso = country_metadata[0] if country_metadata else None
        region = country_metadata[1] if country_metadata else None
        is_remote_raw = item.get("is_remote")
        is_remote = (
            bool(is_remote_raw)
            if isinstance(is_remote_raw, (bool, int))
            else None
        )
        functional_area = _first_string(
            item.get("functional_area_name"),
            item.get("functional_area"),
        )
        raw: dict[str, Any] = {"tenant": tenant, "tld": tld}
        for key in (
            "_id",
            "parent_company_id",
            "designation",
            "department_id",
            "employee_type",
            "employee_sub_type",
            "functional_area",
            "job_tags",
            "capability",
            "salary_range",
            "salary_timeframe",
        ):
            value = item.get(key)
            if value not in (None, "", [], {}):
                raw[key] = value

        return Job(
            url=HttpUrl(f"{base_url}/ms/candidate/careers/{ats_id}"),
            title=title,
            company=self.company_name or tenant,
            ats_type=ATSType.DARWINBOX,
            ats_id=ats_id,
            location=_format_location(item),
            country_iso=country_iso,
            region=region,
            is_remote=is_remote,
            department=_first_string(
                item.get("department_name"),
                item.get("department_name_only"),
                item.get("department"),
            ),
            team=functional_area,
            requisition_id=_coerce_string(item.get("internal_job_code")),
            employment_type=_map_employment_type(commitment),
            commitment=commitment,
            experience=_coerce_int(
                _first_present(item.get("experience_from"), item.get("experience_from_num"))
            ),
            description=_html_to_text(item.get("jd")),
            posted_at=_parse_posted_at(item.get("posted_on"))
            or _parse_iso(item.get("created_on")),
            fetched_at=datetime.now(UTC),
            language="en",
            raw=raw,
        )


def _format_location(item: dict[str, Any]) -> str | None:
    for key in ("tool_tip_locations", "officelocation_show_arr_list"):
        value = item.get(key)
        if isinstance(value, list):
            unique = list(
                dict.fromkeys(
                    entry.strip().rstrip(",")
                    for entry in value
                    if isinstance(entry, str) and entry.strip()
                )
            )
            if unique:
                return "; ".join(unique)
    return _first_string(item.get("locations"), item.get("officelocation_show_arr"))


def _first_string(*values: Any) -> str | None:
    for value in values:
        text = _coerce_string(value)
        if text:
            return text
    return None


def _coerce_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _map_employment_type(value: Any) -> EmploymentType | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized in _TYPE_MAP:
        return _TYPE_MAP[normalized]
    return next(
        (mapped for needle, mapped in _TYPE_MAP.items() if needle in normalized),
        None,
    )


def _parse_posted_at(value: Any) -> datetime | None:
    timestamp = _coerce_int(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OSError, ValueError, OverflowError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _html_to_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    unescaped = html.unescape(value)
    text = _HTML_TAG_RE.sub(" ", unescaped)
    return _WHITESPACE_RE.sub(" ", text).strip()[:25_000] or None
