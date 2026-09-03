"""Ninehire multi-tenant ATS scraper."""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

API_URL = "https://api.ninehire.com/identity-access/homepage/recruitments"
PAGE_SIZE = 100
DESCRIPTION_CONCURRENCY = 4
MAX_PAGES = 10_000

_TENANT_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-"
    r"[a-f0-9]{4}-[a-f0-9]{12}",
    re.IGNORECASE,
)
_ADDRESS_KEY_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_NON_JOB_TITLE_MARKERS = (
    "커피챗",
    "coffee chat",
    "talent pool",
    "인재풀",
    "최종 합격 발표",
    "합격자 발표",
)

_EMPLOYMENT_TYPES: dict[str, EmploymentType] = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contractor": "CONTRACT",
    "contract": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.NINEHIRE)
class NinehireScraper(BaseScraper):
    """Scrape one employer homepage hosted by Ninehire."""

    ats = ATSType.NINEHIRE
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
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
        tenant = company_slug.strip().casefold()
        if not _TENANT_RE.fullmatch(tenant):
            raise ScraperError(
                "NinehireScraper requires a safe ninehire.site tenant slug"
            )
        super().__init__(
            tenant,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.tenant = tenant
        self.base_url = f"https://{tenant}.ninehire.site"
        self.company_name = _clean_text(company_name)
        self.company_id: str | None = None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            await self._bootstrap(fetch)
            jobs = await self._fetch_jobs(fetch)
            if self.include_descriptions:
                await self._hydrate_descriptions(fetch, jobs)
            return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                return await self._fetch_description(fetch, job)

        return self._run_sync(run())

    async def _bootstrap(self, fetch: Fetcher) -> None:
        html = await fetch.get_text(f"{self.base_url}/")
        page_props = _next_page_props(
            html,
            provider=f"Ninehire ({self.tenant}) homepage",
        )
        homepage_props = page_props.get("homepageProps")
        if not isinstance(homepage_props, dict):
            raise ScraperError(
                f"Ninehire ({self.tenant}) homepage has no homepageProps"
            )
        homepage = homepage_props.get("homepage")
        info = homepage_props.get("info")
        domain = homepage_props.get("domain")
        if not isinstance(homepage, dict) or not isinstance(info, dict):
            raise ScraperError(
                f"Ninehire ({self.tenant}) homepage metadata is malformed"
            )
        company_id = _required_uuid(
            homepage.get("companyId"),
            field="companyId",
            tenant=self.tenant,
        )
        info_company_id = _required_uuid(
            info.get("companyId"),
            field="info.companyId",
            tenant=self.tenant,
        )
        if company_id != info_company_id:
            raise ScraperError(
                f"Ninehire ({self.tenant}) company IDs do not match"
            )
        if str(info.get("status")).casefold() != "published":
            raise ScraperError(
                f"Ninehire ({self.tenant}) homepage is not published"
            )
        if isinstance(domain, dict):
            site_url = _clean_text(domain.get("siteUrl"))
            if site_url is not None and site_url.casefold() != self.tenant:
                raise ScraperError(
                    f"Ninehire ({self.tenant}) resolved to tenant {site_url!r}"
                )
        self.company_id = company_id
        self.company_name = (
            self.company_name
            or _required_text(
                info.get("companyName"),
                field="companyName",
                tenant=self.tenant,
            )
        )

    async def _fetch_jobs(self, fetch: Fetcher) -> list[Job]:
        if self.company_id is None or self.company_name is None:
            raise ScraperError(
                f"Ninehire ({self.tenant}) was not bootstrapped"
            )
        expected_total: int | None = None
        total_pages: int | None = None
        raw_count = 0
        seen: set[str] = set()
        jobs: list[Job] = []
        for page in range(1, MAX_PAGES + 1):
            payload = await fetch.get_json(
                API_URL,
                params={
                    "companyId": self.company_id,
                    "page": page,
                    "countPerPage": PAGE_SIZE,
                    "order": "created_at_desc",
                },
                headers={"Referer": f"{self.base_url}/"},
            )
            if not isinstance(payload, dict):
                raise ScraperError(
                    f"Ninehire ({self.tenant}) API returned a non-object"
                )
            page_total = _required_nonnegative_int(
                payload.get("count"),
                field="count",
                tenant=self.tenant,
            )
            results = payload.get("results")
            if not isinstance(results, list):
                raise ScraperError(
                    f"Ninehire ({self.tenant}) API returned no results list"
                )
            if len(results) > PAGE_SIZE:
                raise ScraperError(
                    f"Ninehire ({self.tenant}) returned {len(results)} "
                    f"rows for page size {PAGE_SIZE}"
                )
            if expected_total is None:
                expected_total = page_total
                total_pages = math.ceil(expected_total / PAGE_SIZE)
                if expected_total == 0:
                    if results:
                        raise ScraperError(
                            f"Ninehire ({self.tenant}) returned rows with "
                            "count=0"
                        )
                    return []
            elif page_total != expected_total:
                raise ScraperError(
                    f"Ninehire ({self.tenant}) count changed while paginating"
                )
            if not results:
                raise ScraperError(
                    f"Ninehire ({self.tenant}) pagination stopped at "
                    f"{raw_count} of {expected_total}"
                )
            for item in results:
                if not isinstance(item, dict):
                    raise ScraperError(
                        f"Ninehire ({self.tenant}) returned a malformed job"
                    )
                raw_count += 1
                job = self._parse_job(item)
                if job.ats_id in seen:
                    raise ScraperError(
                        f"Ninehire ({self.tenant}) returned duplicate job "
                        f"ID {job.ats_id}"
                    )
                seen.add(str(job.ats_id))
                if _is_real_job(job.title):
                    jobs.append(job)
            if total_pages is not None and page >= total_pages:
                break
        else:
            raise ScraperError(
                f"Ninehire ({self.tenant}) reached the {MAX_PAGES}-page "
                "safety cap"
            )
        if expected_total is None or raw_count != expected_total:
            raise ScraperError(
                f"Ninehire ({self.tenant}) expected {expected_total} jobs, "
                f"parsed {raw_count}"
            )
        return jobs

    def _parse_job(self, item: dict[str, Any]) -> Job:
        if self.company_name is None or self.company_id is None:
            raise ScraperError(
                f"Ninehire ({self.tenant}) was not bootstrapped"
            )
        item_company_id = _required_uuid(
            item.get("companyId"),
            field="job.companyId",
            tenant=self.tenant,
        )
        if item_company_id != self.company_id:
            raise ScraperError(
                f"Ninehire ({self.tenant}) returned another company's job"
            )
        recruitment_id = _required_uuid(
            item.get("recruitmentId"),
            field="recruitmentId",
            tenant=self.tenant,
        )
        address_key = _required_address_key(
            item.get("addressKey"),
            tenant=self.tenant,
        )
        title = _required_text(
            item.get("title") or item.get("externalTitle"),
            field="title",
            tenant=self.tenant,
        )
        locations = _parse_locations(item.get("jobLocations"))
        location = ", ".join(locations["names"]) or None
        country_iso, region = _location_geography(location)
        employment_values = _string_list(item.get("employmentType"))
        career = item.get("career")
        career_range = (
            career.get("range")
            if isinstance(career, dict)
            and isinstance(career.get("range"), dict)
            else None
        )
        job_group = item.get("jobGroup")
        job_task = item.get("jobTask")
        affiliation = item.get("affiliation")
        canonical_url = f"{self.base_url}/job_posting/{address_key}"
        return Job(
            url=canonical_url,
            title=title,
            company=self.company_name,
            ats_type=ATSType.NINEHIRE,
            ats_id=recruitment_id,
            location=location,
            country_iso=country_iso,
            region=region,
            lat=locations["lat"],
            lon=locations["lon"],
            is_remote=_is_remote(title, location),
            experience=(
                _coerce_int(career_range.get("over"))
                if career_range is not None
                else None
            ),
            employment_type=_employment_type(employment_values),
            department=_nested_text(job_group, "title"),
            team=_nested_text(job_task, "title"),
            apply_url=canonical_url,
            commitment=", ".join(employment_values) or None,
            posted_at=_parse_datetime(item.get("createdAt")),
            fetched_at=datetime.now(UTC),
            language=_language_for_text(title),
            raw={
                "address_key": address_key,
                "company_id": self.company_id,
                "status": item.get("status"),
                "deadline_type": item.get("deadlineType"),
                "deadline_value": item.get("deadlineValue"),
                "career": career,
                "affiliation": _nested_text(affiliation, "title"),
                "tags": _tag_values(item.get("tags")),
                "always_exposure": item.get("alwaysExposure"),
            },
        )

    async def _hydrate_descriptions(
        self,
        fetch: Fetcher,
        jobs: list[Job],
    ) -> None:
        semaphore = asyncio.Semaphore(DESCRIPTION_CONCURRENCY)

        async def hydrate(job: Job) -> None:
            async with semaphore:
                try:
                    description = await self._fetch_description(fetch, job)
                except ScraperError:
                    return
                if description:
                    job.description = description[:25_000]

        await asyncio.gather(*(hydrate(job) for job in jobs))

    async def _fetch_description(
        self,
        fetch: Fetcher,
        job: Job,
    ) -> str | None:
        html = await fetch.get_text(str(job.url))
        page_props = _next_page_props(
            html,
            provider=f"Ninehire job {job.ats_id}",
        )
        recruitment = page_props.get("recruitment")
        job_posting = page_props.get("jobPosting")
        if not isinstance(recruitment, dict) or not isinstance(
            job_posting,
            dict,
        ):
            raise ScraperError(
                f"Ninehire job {job.ats_id} has malformed detail metadata"
            )
        detail_id = _required_uuid(
            recruitment.get("recruitmentId"),
            field="detail.recruitmentId",
            tenant=self.tenant,
        )
        if detail_id != job.ats_id:
            raise ScraperError(
                f"Ninehire job {job.ats_id} detail returned ID {detail_id}"
            )
        detail_key = _required_address_key(
            recruitment.get("addressKey"),
            tenant=self.tenant,
        )
        if detail_key != job.raw.get("address_key"):
            raise ScraperError(
                f"Ninehire job {job.ats_id} detail address key changed"
            )
        if job_posting.get("isActive") is not True:
            return None
        return _clean_multiline(job_posting.get("content"))


def _next_page_props(html: str, *, provider: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        raise ScraperError(f"{provider} has no Next.js bootstrap data")
    try:
        payload = json.loads(script.string)
        page_props = payload["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ScraperError(
            f"{provider} has malformed Next.js bootstrap data"
        ) from exc
    if not isinstance(page_props, dict):
        raise ScraperError(f"{provider} has no pageProps object")
    return page_props


def _parse_locations(value: object) -> dict[str, Any]:
    if not isinstance(value, list):
        return {"names": [], "lat": None, "lon": None}
    names: list[str] = []
    coordinates: list[tuple[float, float]] = []
    for location in value:
        if not isinstance(location, dict):
            continue
        name = (
            _clean_text(location.get("addressName"))
            or _clean_text(location.get("placeName"))
        )
        if name and name not in names:
            names.append(name)
        lat = _coerce_float(location.get("y"))
        lon = _coerce_float(location.get("x"))
        if (
            lat is not None
            and lon is not None
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            coordinates.append((lat, lon))
    return {
        "names": names,
        "lat": coordinates[0][0] if len(coordinates) == 1 else None,
        "lon": coordinates[0][1] if len(coordinates) == 1 else None,
    }


def _required_uuid(value: object, *, field: str, tenant: str) -> str:
    cleaned = _clean_text(value)
    if cleaned is None or not _UUID_RE.fullmatch(cleaned):
        raise ScraperError(
            f"Ninehire ({tenant}) returned malformed {field}={value!r}"
        )
    return cleaned.casefold()


def _required_address_key(value: object, *, tenant: str) -> str:
    cleaned = _clean_text(value)
    if cleaned is None or not _ADDRESS_KEY_RE.fullmatch(cleaned):
        raise ScraperError(
            f"Ninehire ({tenant}) returned malformed addressKey={value!r}"
        )
    return cleaned


def _required_text(
    value: object,
    *,
    field: str,
    tenant: str,
) -> str:
    cleaned = _clean_text(value)
    if cleaned is None:
        raise ScraperError(f"Ninehire ({tenant}) returned no {field}")
    return cleaned


def _required_nonnegative_int(
    value: object,
    *,
    field: str,
    tenant: str,
) -> int:
    parsed = _coerce_int(value)
    if parsed is None or parsed < 0:
        raise ScraperError(
            f"Ninehire ({tenant}) returned malformed {field}={value!r}"
        )
    return parsed


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized or None


def _clean_multiline(value: object) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(
        "\n",
        strip=True,
    )
    lines = [
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in text.splitlines()
    ]
    return "\n".join(line for line in lines if line) or None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        cleaned
        for item in value
        if (cleaned := _clean_text(item)) is not None
    ]


def _nested_text(value: object, field: str) -> str | None:
    return (
        _clean_text(value.get(field))
        if isinstance(value, dict)
        else None
    )


def _tag_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        cleaned
        for tag in value
        if isinstance(tag, dict)
        and (cleaned := _clean_text(tag.get("content"))) is not None
    ]


def _employment_type(values: list[str]) -> EmploymentType | None:
    return next(
        (
            normalized
            for value in values
            if (normalized := _EMPLOYMENT_TYPES.get(value.casefold()))
            is not None
        ),
        None,
    )


def _location_geography(
    location: str | None,
) -> tuple[str | None, str | None]:
    normalized = (location or "").casefold()
    if any(
        marker in normalized
        for marker in (
            "대한민국",
            "서울",
            "부산",
            "대구",
            "인천",
            "광주",
            "대전",
            "울산",
            "세종",
            "경기",
            "강원",
            "충북",
            "충남",
            "전북",
            "전남",
            "경북",
            "경남",
            "제주",
            "south korea",
            "korea",
        )
    ):
        return "KR", "Asia"
    if any(
        marker in normalized
        for marker in ("일본", "japan", "tokyo", "osaka")
    ):
        return "JP", "Asia"
    if any(
        marker in normalized
        for marker in (
            "united states",
            "usa",
            "california",
            "new york",
            "massachusetts",
        )
    ):
        return "US", "North America"
    return None, None


def _is_remote(title: str, location: str | None) -> bool | None:
    combined = f"{title} {location or ''}".casefold()
    if any(
        marker in combined
        for marker in ("remote", "재택", "원격", "리모트")
    ):
        return True
    return None


def _parse_datetime(value: object) -> datetime | None:
    cleaned = _clean_text(value)
    if cleaned is None:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None
        else parsed.astimezone(UTC)
    )


def _language_for_text(value: str) -> str | None:
    if re.search(r"[\uac00-\ud7af]", value):
        return "ko"
    if re.search(r"[A-Za-z]", value):
        return "en"
    return None


def _is_real_job(title: str) -> bool:
    normalized = title.casefold()
    return not any(
        marker in normalized for marker in _NON_JOB_TITLE_MARKERS
    )
