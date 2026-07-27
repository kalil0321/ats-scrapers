"""ADP Workforce Now public careers scraper.

ADP Workforce Now career sites use URLs shaped like::

    https://workforcenow.adp.com/mascsr/default/mdf/recruitment/
    recruitment.html?cid={tenant}&ccId={career-center}&lang=en_US

The page calls unauthenticated JSON endpoints under
``/mascsr/default/careercenter/public/events/staffing/v1``. Listings expose
titles, locations, salary ranges, worker categories, and an external job ID.
The per-job endpoint adds the full requisition description.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

PAGE_LIMIT = 100
DETAIL_CONCURRENCY = 8
CAREERS_PATH = "/mascsr/default/mdf/recruitment/recruitment.html"
API_ROOT = "/mascsr/default/careercenter/public/events/staffing/v1"
LISTING_PATH = f"{API_ROOT}/job-requisitions"

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SUPPORTED_HOSTS = {
    "workforcenow.adp.com",
    "workforcenow.cloud.adp.com",
}

_EMPLOYMENT_TYPE_PATTERNS: tuple[tuple[str, EmploymentType], ...] = (
    ("intern", "INTERN"),
    ("temporary", "TEMPORARY"),
    ("seasonal", "TEMPORARY"),
    ("contract", "CONTRACT"),
    ("part-time", "PART_TIME"),
    ("part time", "PART_TIME"),
    ("full-time", "FULL_TIME"),
    ("full time", "FULL_TIME"),
    ("regular", "FULL_TIME"),
)

_SALARY_PERIODS: dict[str, SalaryPeriod] = {
    "AN": "YEAR",
    "ANNUAL": "YEAR",
    "ANNUALLY": "YEAR",
    "YEAR": "YEAR",
    "HR": "HOUR",
    "HOURLY": "HOUR",
    "HOUR": "HOUR",
    "DA": "DAY",
    "DAILY": "DAY",
    "DAY": "DAY",
    "WK": "WEEK",
    "WEEKLY": "WEEK",
    "WEEK": "WEEK",
    "MO": "MONTH",
    "MONTHLY": "MONTH",
    "MONTH": "MONTH",
}

_REGION_BY_COUNTRY = {
    "CA": "North America",
    "MX": "North America",
    "US": "North America",
    "AU": "Oceania",
    "NZ": "Oceania",
    "CN": "Asia",
    "HK": "Asia",
    "IN": "Asia",
    "JP": "Asia",
    "KR": "Asia",
    "SG": "Asia",
    "AT": "Europe",
    "BE": "Europe",
    "CH": "Europe",
    "CZ": "Europe",
    "DE": "Europe",
    "DK": "Europe",
    "ES": "Europe",
    "FI": "Europe",
    "FR": "Europe",
    "GB": "Europe",
    "IE": "Europe",
    "IT": "Europe",
    "NL": "Europe",
    "NO": "Europe",
    "PL": "Europe",
    "PT": "Europe",
    "SE": "Europe",
}


@dataclass(frozen=True)
class ADPTarget:
    origin: str
    host: str
    cid: str
    career_center_id: str
    language: str

    @property
    def listing_url(self) -> str:
        return f"{self.origin}{LISTING_PATH}"

    def detail_url(self, external_job_id: str) -> str:
        return f"{self.listing_url}/{external_job_id}"

    def query(self) -> dict[str, str]:
        return {
            "cid": self.cid,
            "ccId": self.career_center_id,
            "lang": self.language,
            "locale": self.language,
        }

    def public_job_url(self, item_id: str) -> str:
        query = {
            "cid": self.cid,
            "ccId": self.career_center_id,
            "lang": self.language,
            "jobId": item_id,
            "source": "CC2",
        }
        return f"{self.origin}{CAREERS_PATH}?{urlencode(query)}"


@ScraperRegistry.register(ATSType.ADP)
class ADPWorkforceNowScraper(BaseScraper):
    """Scrape a public ADP Workforce Now career center.

    ``company_slug`` is the full public recruitment URL containing ``cid`` and
    ``ccId`` query parameters.
    """

    ats = ATSType.ADP
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        company_name: str | None = None,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_name = company_name

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        target = _parse_target(self.company_slug)
        copy = job.model_copy()

        async def run() -> str | None:
            async with self.make_fetcher(
                headers=_headers(target),
            ) as fetch:
                semaphore = asyncio.Semaphore(1)
                await self._enrich_detail(fetch, semaphore, target, copy)
            return copy.description

        return self._run_sync(run())

    async def afetch(self) -> list[Job]:
        target = _parse_target(self.company_slug)
        jobs: list[Job] = []
        by_id: dict[str, Job] = {}
        offset = 0

        async with self.make_fetcher(headers=_headers(target)) as fetch:
            while True:
                payload = await fetch.get_json(
                    target.listing_url,
                    params={
                        **target.query(),
                        "$skip": offset,
                        "$top": PAGE_LIMIT,
                        "userQuery": "",
                    },
                )
                items, total = _unwrap_listing(payload)
                if not items:
                    if offset < total:
                        raise ScraperError(
                            "ADP pagination ended before the advertised total "
                            f"({offset} of {total})"
                        )
                    break

                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ScraperError(
                            f"ADP listing row {offset + index} was not an object"
                        )
                    job = self._parse_job(item, target)
                    existing = by_id.get(job.ats_id or "")
                    if existing is not None:
                        if (
                            existing.title != job.title
                            or str(existing.url) != str(job.url)
                        ):
                            raise ScraperError(
                                f"ADP returned conflicting duplicate job id {job.ats_id!r}"
                            )
                        continue
                    by_id[job.ats_id or ""] = job
                    jobs.append(job)

                offset += len(items)
                if offset >= total:
                    break

            if self.include_descriptions and jobs:
                semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(
                    self._enrich_detail(fetch, semaphore, target, job)
                    for job in jobs
                ))

        return jobs

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        target: ADPTarget,
        job: Job,
    ) -> None:
        if job.description:
            return
        external_job_id = _raw_string(job, "external_job_id")
        item_id = _raw_string(job, "item_id")
        if not external_job_id:
            return
        async with semaphore:
            try:
                detail = await fetch.get_json(
                    target.detail_url(external_job_id),
                    params=target.query(),
                )
            except ScraperError:
                return
        if not isinstance(detail, dict):
            return
        detail_item_id = _string(detail.get("itemID"))
        if not item_id or detail_item_id != item_id:
            return
        description = detail.get("requisitionDescription")
        if isinstance(description, str) and description.strip():
            job.description = _strip_html(description)[:25_000] or None

    def _parse_job(self, item: dict[str, Any], target: ADPTarget) -> Job:
        item_id = _required_string(item, "itemID")
        title = _required_string(item, "requisitionTitle")
        ats_id = f"{target.cid}:{target.career_center_id}:{item_id}"
        locations = _locations(item)
        location = "; ".join(locations) or None
        country_iso = _country_iso(item, locations)
        worker_category = _nested_string(item, "workLevelCode", "shortName")
        employment_type = _employment_type(worker_category)
        salary_min, salary_max, salary_currency = _salary_range(item)
        salary_type_code, salary_type_label = _code_field(
            item,
            "SalaryType",
        )
        salary_period = _salary_period(salary_type_code, salary_type_label)
        salary_summary = _string_field(item, "SalaryRange")
        external_job_id = _string_field(item, "ExternalJobID")
        department = _string_field(item, "HomeDepartment")
        job_class = _string_field(item, "JobClass")

        raw: dict[str, object] = {"item_id": item_id}
        if external_job_id:
            raw["external_job_id"] = external_job_id
        if len(locations) > 1:
            raw["all_locations"] = locations
        if job_class:
            raw["job_class"] = job_class
        if salary_type_code:
            raw["salary_type_code"] = salary_type_code
        if salary_type_label:
            raw["salary_type_label"] = salary_type_label

        return Job(
            url=HttpUrl(target.public_job_url(item_id)),
            title=title,
            company=self.company_name or target.cid,
            ats_type=ATSType.ADP,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region=_REGION_BY_COUNTRY.get(country_iso or ""),
            language=target.language.split("_", 1)[0].lower(),
            is_remote=(
                True
                if any("remote" in value.casefold() for value in locations)
                else None
            ),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=salary_currency,
            salary_period=salary_period,
            salary_summary=salary_summary,
            employment_type=employment_type,
            department=department,
            commitment=worker_category,
            requisition_id=_string(item.get("clientRequisitionID")),
            posted_at=_parse_iso(_string(item.get("postDate"))),
            fetched_at=datetime.now(UTC),
            raw=raw,
        )


def _parse_target(raw_url: str) -> ADPTarget:
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or host not in _SUPPORTED_HOSTS:
        raise ScraperError(
            "ADP slug must be a public workforcenow.adp.com recruitment URL"
        )
    query = parse_qs(parsed.query)
    cid = _first_query_value(query, "cid")
    career_center_id = _first_query_value(query, "ccId")
    language = _first_query_value(query, "lang")
    if not language or language.casefold() in {"none", "null", "undefined"}:
        language = "en_US"
    if not cid or not career_center_id:
        raise ScraperError("ADP recruitment URL must include cid and ccId")
    return ADPTarget(
        origin=f"{parsed.scheme}://{parsed.netloc}",
        host=host,
        cid=cid,
        career_center_id=career_center_id,
        language=language,
    )


def _headers(target: ADPTarget) -> dict[str, str]:
    return {
        **ADPWorkforceNowScraper.default_headers,
        "Accept-Language": target.language,
        "locale": target.language,
        "x-forwarded-host": target.host,
    }


def _unwrap_listing(payload: object) -> tuple[list[object], int]:
    if not isinstance(payload, dict):
        raise ScraperError("ADP listing response was not an object")
    items = payload.get("jobRequisitions")
    meta = payload.get("meta")
    if not isinstance(items, list) or not isinstance(meta, dict):
        raise ScraperError("ADP listing response omitted jobRequisitions or meta")
    total = meta.get("totalNumber")
    if not isinstance(total, int) or total < 0:
        raise ScraperError("ADP listing response omitted a valid totalNumber")
    return items, total


def _locations(item: dict[str, Any]) -> list[str]:
    raw_locations = item.get("requisitionLocations")
    if not isinstance(raw_locations, list):
        return []
    locations: list[str] = []
    for raw in raw_locations:
        if not isinstance(raw, dict):
            continue
        value = _nested_string(raw, "nameCode", "shortName")
        if value and value not in locations:
            locations.append(value)
    return locations


def _country_iso(item: dict[str, Any], locations: list[str]) -> str | None:
    raw_locations = item.get("requisitionLocations")
    if isinstance(raw_locations, list):
        for raw in raw_locations:
            if not isinstance(raw, dict):
                continue
            address = raw.get("address")
            if not isinstance(address, dict):
                continue
            country = _nested_string(address, "countryCode", "codeValue")
            if country and len(country) == 2:
                return country.upper()
    for location in locations:
        tail = location.rsplit(",", 1)[-1].strip().upper()
        if len(tail) == 2 and tail.isalpha():
            return tail
    return None


def _salary_range(
    item: dict[str, Any],
) -> tuple[float | None, float | None, str | None]:
    pay_range = item.get("payGradeRange")
    if not isinstance(pay_range, dict):
        return None, None, None
    minimum = pay_range.get("minimumRate")
    maximum = pay_range.get("maximumRate")
    min_value = _amount(minimum)
    max_value = _amount(maximum)
    currency = None
    for rate in (minimum, maximum):
        if isinstance(rate, dict):
            raw_currency = _string(rate.get("currencyCode"))
            if raw_currency and len(raw_currency) == 3:
                currency = raw_currency.upper()
                break
    if (
        min_value is not None
        and max_value is not None
        and max_value > 0
        and min_value > max_value
    ):
        raise ScraperError("ADP returned an inverted positive salary range")
    return min_value, max_value, currency


def _amount(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    amount = value.get("amountValue")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return float(amount)


def _salary_period(
    code: str | None,
    label: str | None,
) -> SalaryPeriod | None:
    for value in (code, label):
        if value and (period := _SALARY_PERIODS.get(value.strip().upper())):
            return period
    return None


def _employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    normalized = value.casefold()
    for needle, employment_type in _EMPLOYMENT_TYPE_PATTERNS:
        if needle in normalized:
            return employment_type
    return None


def _custom_fields(item: dict[str, Any], field_type: str) -> list[dict[str, Any]]:
    group = item.get("customFieldGroup")
    if not isinstance(group, dict):
        return []
    fields = group.get(field_type)
    if not isinstance(fields, list):
        return []
    return [field for field in fields if isinstance(field, dict)]


def _string_field(item: dict[str, Any], code: str) -> str | None:
    for field in _custom_fields(item, "stringFields"):
        if _nested_string(field, "nameCode", "codeValue") == code:
            return _string(field.get("stringValue"))
    return None


def _code_field(
    item: dict[str, Any],
    code: str,
) -> tuple[str | None, str | None]:
    for field in _custom_fields(item, "codeFields"):
        if _nested_string(field, "nameCode", "codeValue") == code:
            return _string(field.get("codeValue")), _string(field.get("shortName"))
    return None, None


def _raw_string(job: Job, key: str) -> str | None:
    if not job.raw:
        return None
    return _string(job.raw.get(key))


def _nested_string(value: object, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _string(current)


def _required_string(item: dict[str, Any], key: str) -> str:
    value = _string(item.get(key))
    if not value:
        raise ScraperError(f"ADP listing row omitted {key}")
    return value


def _string(value: object) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    return normalized or None


def _first_query_value(
    query: dict[str, list[str]],
    key: str,
) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return _string(values[0])


def _strip_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value)
    text = _SPACE_RE.sub(" ", html_mod.unescape(text)).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
