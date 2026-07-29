"""Beisen / iTalent (zhiye.com) multi-tenant ATS scraper.

Current Beisen career sites live at ``https://{tenant}.zhiye.com``. The
public SPA exposes its portal identifier in an inline ``BSGlobal`` object,
then lists jobs through ``POST /api/Jobad/GetJobAdPageList``.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

REGISTER_PATH = "/portal/registerSystemInfo"
SEARCH_PATH = "/api/Jobad/GetJobAdPageList"
PAGE_SIZE = 1000
MAX_PAGES = 1000

_TENANT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BSGLOBAL_RE = re.compile(r"var\s+BSGlobal\s*=\s*(\{)", re.DOTALL)
_TENANT_ID_RE = re.compile(
    r"(?:stcms\.beisen\.com/image|portal-oss\.zhiye\.com)/(\d+)"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(?:div|li|p|tr|h[1-6])\s*>", re.IGNORECASE)
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HORIZONTAL_SPACE_RE = re.compile(r"[^\S\r\n]+")
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")
_RECRUIT_SUFFIXES = (
    "校园招聘",
    "社会招聘",
    "人才招聘",
    "招贤纳士",
    "招聘门户",
    "招聘官网",
    "招聘网站",
    "招聘中心",
    "招聘",
)


@ScraperRegistry.register(ATSType.BEISEN)
class BeisenScraper(BaseScraper):
    """Scrape a current-generation Beisen tenant by zhiye.com subdomain."""

    ats = ATSType.BEISEN
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        business_type: int | None = None,
        language: str = "zh",
        country_iso: str = "CN",
        company_name: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        slug = company_slug.strip().lower()
        if not _TENANT_RE.fullmatch(slug):
            raise ScraperError(
                f"Beisen slug {company_slug!r} looks malformed; "
                "expected a DNS-safe zhiye.com subdomain"
            )
        if business_type not in {None, 0, 1, 2}:
            raise ScraperError("Beisen business_type must be 0, 1, 2, or None")
        self.slug = slug
        self.business_type = business_type
        self.language = language
        self.country_iso = country_iso.upper()
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )
        self.base_url = f"https://{slug}.zhiye.com"
        self.portal_id: str | None = None
        self.tenant_id: str | None = None
        self.tenant_name: str | None = None
        self.key: str | None = None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            await self._resolve_tenant(fetch)
            jobs: list[Job] = []
            seen: set[str] = set()
            total: int | None = None
            for page_index in range(MAX_PAGES):
                payload = await self._fetch_page(fetch, page_index)
                items = payload.get("Data")
                if not isinstance(items, list) or not items:
                    break

                new_count = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_job(item)
                    if job is None or not job.ats_id or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                    new_count += 1

                total = _coerce_int(payload.get("Count"))
                if total is not None and len(seen) >= total:
                    break
                if len(items) < PAGE_SIZE:
                    break
                if new_count == 0:
                    raise ScraperError(
                        "Beisen pagination repeated a full page without new jobs "
                        f"at page={page_index} for {self.slug!r}"
                    )
            else:
                raise ScraperError(
                    "Beisen pagination reached the safety cap "
                    f"({MAX_PAGES} pages, {len(jobs)} of {total or 'unknown'} jobs)"
                )
        return jobs

    async def _resolve_tenant(self, fetch: Fetcher) -> None:
        text = await fetch.get_text(
            f"{self.base_url}{REGISTER_PATH}",
            headers={"Accept": "text/html,application/xhtml+xml,*/*"},
        )
        match = _BSGLOBAL_RE.search(text)
        if not match:
            raise ScraperError(
                f"Beisen tenant {self.slug!r} did not return a BSGlobal config; "
                "it may use the legacy portal"
            )
        try:
            config, _end = json.JSONDecoder().raw_decode(text, match.start(1))
        except ValueError as exc:
            raise ScraperError(
                f"Beisen BSGlobal for {self.slug!r} was not valid JSON: {exc}"
            ) from exc
        if not isinstance(config, dict):
            raise ScraperError(f"Beisen BSGlobal for {self.slug!r} was not an object")

        portal_id = _clean_text(config.get("PortalId"))
        if not portal_id:
            raise ScraperError(
                f"Beisen tenant {self.slug!r} is missing PortalId in BSGlobal"
            )
        tenant_ids = _TENANT_ID_RE.findall(text)
        self.portal_id = portal_id
        self.key = _clean_text(config.get("Key"))
        self.tenant_id = next(
            (tenant_id for tenant_id in tenant_ids if tenant_id != "10000"),
            tenant_ids[0] if tenant_ids else None,
        )
        filing = config.get("BeiAnInfo")
        site_name = filing.get("SiteName") if isinstance(filing, dict) else None
        self.tenant_name = _clean_company_name(site_name) or self.slug

    async def _fetch_page(self, fetch: Fetcher, page_index: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "PageIndex": page_index,
            "PageSize": PAGE_SIZE,
            "KeyWords": "",
            "SpecialType": 0,
            "PortalId": self.portal_id,
            "DisplayFields": [
                "Category",
                "Description",
                "LocId",
                "Org",
                "Salary",
            ],
        }
        if self.business_type is not None:
            body["Category"] = [str(self.business_type)]
        payload = await fetch.post_json(
            f"{self.base_url}{SEARCH_PATH}",
            json=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": f"{self.base_url}/social/jobs",
            },
        )
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Beisen returned a non-object payload at page={page_index}"
            )
        if payload.get("Code") not in {200, "200"}:
            raise ScraperError(
                "Beisen API failure "
                f"at page={page_index} for {self.slug!r}: {payload!r}"
            )
        return payload

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("JobAdId")
        ats_id = str(raw_id).strip() if raw_id is not None else ""
        title = _clean_text(item.get("JobAdName"))
        if not ats_id or not title:
            return None

        raw: dict[str, Any] = {
            "tenant": self.slug,
            "portal_id": self.portal_id,
        }
        if self.tenant_id:
            raw["tenant_id"] = self.tenant_id
        if self.key:
            raw["key"] = self.key
        for source, destination in (
            ("Id", "internal_id"),
            ("OrgId", "org_id"),
            ("Org", "org"),
            ("Category", "category"),
            ("CategoryId", "category_id"),
            ("LocId", "loc_id"),
            ("Station", "station"),
            ("Kind", "kind"),
            ("Degree", "degree"),
            ("YearsOfWorking", "years_of_working"),
            ("HeadCount", "head_count"),
            ("ClassificationOne", "classification_1"),
            ("ClassificationTwo", "classification_2"),
            ("Classification3", "classification_3"),
            ("Classification4", "classification_4"),
        ):
            value = item.get(source)
            if value not in (None, "", 0, []):
                raw[destination] = value

        description = None
        if self.include_descriptions:
            description = _join_nonempty(
                _clean_html(item.get("Duty")),
                _clean_html(item.get("Require")),
                separator="\n\n",
            )
        return Job(
            url=HttpUrl(f"{self.base_url}/portal/jobs/{ats_id}"),
            title=title,
            company=self.company_name or self.tenant_name or self.slug,
            ats_type=ATSType.BEISEN,
            ats_id=ats_id,
            location=_format_location(item.get("LocNames"))
            or _clean_text(item.get("DetailAddress")),
            country_iso=self.country_iso,
            region="Asia" if self.country_iso in {"CN", "HK", "MO", "TW"} else None,
            description=description,
            salary_summary=_clean_text(item.get("Salary")),
            department=_clean_text(item.get("Org"))
            or _clean_text(item.get("Category")),
            posted_at=_parse_date(item.get("ChangeDate"))
            or _parse_date(item.get("PostDate")),
            fetched_at=datetime.now(UTC),
            language=self.language,
            raw=raw,
        )


def _clean_company_name(value: object) -> str:
    name = _clean_text(value) or ""
    for suffix in _RECRUIT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean_html(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_HORIZONTAL_SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text).strip()
    return text or None


def _join_nonempty(*values: str | None, separator: str) -> str | None:
    parts = [value for value in values if value]
    return separator.join(parts) if parts else None


def _format_location(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    parts = [str(part).strip() for part in value if str(part).strip()]
    return ", ".join(parts) if parts else None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: object) -> datetime | None:
    text = _clean_text(value)
    if text is None or text.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
