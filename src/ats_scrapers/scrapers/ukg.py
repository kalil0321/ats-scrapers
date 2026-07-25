"""UKG Pro Recruiting public careers scraper."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

from pydantic import HttpUrl

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PAGE_SIZE = 50
DETAIL_CONCURRENCY = 8
_HOST_RE = re.compile(r"^recruiting\d*\.ultipro\.(?:com|ca)$", re.IGNORECASE)
_TENANT_RE = re.compile(r"^[A-Za-z0-9]+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LOAD_URL_RE = re.compile(r'\bloadUrl:\s*"([^"]+)"')
_DETAIL_MARKER = "new US.Opportunity.CandidateOpportunityDetail("
_COUNTRY_CODES = {
    "AUS": "AU",
    "AUT": "AT",
    "BEL": "BE",
    "BRA": "BR",
    "CAN": "CA",
    "CHE": "CH",
    "CHN": "CN",
    "DEU": "DE",
    "DNK": "DK",
    "ESP": "ES",
    "FIN": "FI",
    "FRA": "FR",
    "GBR": "GB",
    "IND": "IN",
    "IRL": "IE",
    "ITA": "IT",
    "JPN": "JP",
    "MEX": "MX",
    "NLD": "NL",
    "NOR": "NO",
    "NZL": "NZ",
    "POL": "PL",
    "PRT": "PT",
    "SWE": "SE",
    "USA": "US",
}
logger = logging.getLogger(__name__)


@ScraperRegistry.register(ATSType.UKG)
class UKGProScraper(BaseScraper):
    """Scrape one public UKG Pro Recruiting job board."""

    ats = ATSType.UKG
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0",
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
        self.board_url = _normalize_board_url(company_slug)
        parsed = urlparse(self.board_url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        self.host = parsed.hostname or ""
        self.tenant = segments[0]
        self.board_id = segments[2]
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else self.tenant
        )

    async def afetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        reported_total: int | None = None

        async with self.make_fetcher() as fetch:
            board_html = await fetch.get_text(self.board_url)
            internal_base = self._parse_internal_base(board_html)
            load_url = f"{internal_base}/JobBoardView/LoadSearchResults"
            skip = 0

            while True:
                payload = await fetch.post_json(
                    load_url,
                    json={"opportunitySearch": _search_payload(skip)},
                    headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                page_jobs, total = self._parse_listing(
                    payload,
                    internal_base=internal_base,
                    skip=skip,
                )
                if reported_total is None:
                    reported_total = total
                elif total != reported_total:
                    raise ScraperError(
                        "UKG total changed during pagination "
                        f"({reported_total} to {total})"
                    )
                for job in page_jobs:
                    if not job.ats_id or job.ats_id in seen_ids:
                        raise ScraperError(
                            f"UKG returned duplicate job id {job.ats_id!r}"
                        )
                    seen_ids.add(job.ats_id)
                    jobs.append(job)
                skip += len(page_jobs)
                if skip >= total:
                    break
                if not page_jobs:
                    raise ScraperError(
                        f"UKG ({self.tenant}) pagination ended before total"
                    )

            if reported_total is None or len(jobs) != reported_total:
                raise ScraperError(
                    "UKG catalogue ended before the reported total "
                    f"({len(jobs)}/{reported_total})"
                )
            if not self.include_descriptions or not jobs:
                return jobs

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            enriched = await asyncio.gather(
                *(self._enrich_detail(fetch, semaphore, job) for job in jobs)
            )

        completed = [job for job in enriched if job is not None]
        if jobs and not completed:
            raise ScraperError(
                f"UKG ({self.tenant}) lost every listed job "
                "during detail validation"
            )
        return completed

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                enriched = await self._enrich_detail(
                    fetch,
                    asyncio.Semaphore(1),
                    copy,
                )
            return enriched.description if enriched is not None else None

        return self._run_sync(run())

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        job: Job,
    ) -> Job | None:
        try:
            async with semaphore:
                response = await fetch.request(
                    "GET",
                    str(job.url),
                    handled={404, 410},
                )
            if response.status_code in {404, 410}:
                return None
            detail = _extract_detail_payload(response.text)
            if detail.get("OpportunityIsClosed") is True:
                return None
            if detail.get("Id") != job.ats_id:
                raise ScraperError(
                    f"UKG detail id did not match listing id {job.ats_id}"
                )
            _apply_detail(job, detail)
        except ScraperError as exc:
            logger.warning(
                "Keeping UKG listing job %s after optional detail failure: %s",
                job.ats_id,
                exc,
            )
            return job
        if not job.description:
            logger.warning(
                "Keeping UKG job %s although its detail page omitted "
                "a description",
                job.ats_id,
            )
        return job

    def _parse_internal_base(self, html_text: str) -> str:
        match = _LOAD_URL_RE.search(html_text)
        if match is None:
            raise ScraperError(
                f"UKG ({self.tenant}) omitted its public search endpoint"
            )
        load_url = urljoin(f"https://{self.host}/", html.unescape(match.group(1)))
        parsed = urlparse(load_url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.host
            or len(segments) != 5
            or segments[0].casefold() != self.tenant.casefold()
            or segments[1].casefold() != "jobboard"
            or _UUID_RE.fullmatch(segments[2]) is None
            or segments[3:] != ["JobBoardView", "LoadSearchResults"]
        ):
            raise ScraperError(
                f"UKG ({self.tenant}) returned an unsafe search endpoint"
            )
        return (
            f"https://{self.host}/{segments[0]}/JobBoard/{segments[2]}"
        )

    def _parse_listing(
        self,
        payload: object,
        *,
        internal_base: str,
        skip: int,
    ) -> tuple[list[Job], int]:
        if not isinstance(payload, dict):
            raise ScraperError(f"UKG ({self.tenant}) returned invalid JSON")
        items = payload.get("opportunities")
        total = payload.get("totalCount")
        if (
            not isinstance(items, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
        ):
            raise ScraperError(
                f"UKG ({self.tenant}) returned an invalid result envelope"
            )
        expected = min(PAGE_SIZE, max(total - skip, 0))
        if len(items) != expected:
            raise ScraperError(
                "UKG listing count did not match the reported total "
                f"({len(items)} rows at offset {skip}, expected {expected})"
            )
        jobs = [
            self._job_from_listing(item, internal_base=internal_base)
            for item in items
        ]
        return jobs, total

    def _job_from_listing(
        self,
        item: object,
        *,
        internal_base: str,
    ) -> Job:
        if not isinstance(item, dict):
            raise ScraperError(f"UKG ({self.tenant}) returned a non-object job")
        job_id = _required_string(item, "Id")
        title = _required_string(item, "Title")
        if _UUID_RE.fullmatch(job_id) is None:
            raise ScraperError(f"UKG returned an invalid job id {job_id!r}")

        location, country_iso, lat, lon = _locations(item.get("Locations"))
        location_type = item.get("JobLocationType")
        is_remote = (
            True
            if location_type == 2
            or (location is not None and "remote" in location.casefold())
            else None
        )
        full_time = item.get("FullTime")
        employment_type = (
            "FULL_TIME"
            if full_time is True
            else "PART_TIME" if full_time is False else None
        )
        detail_url = (
            f"{internal_base}/OpportunityDetail?opportunityId={job_id}"
        )
        requisition = item.get("RequisitionNumber")
        department = item.get("JobCategoryName")
        return Job(
            url=HttpUrl(detail_url),
            title=title,
            company=self.company_name,
            ats_type=ATSType.UKG,
            ats_id=job_id,
            location=location,
            country_iso=country_iso,
            lat=lat,
            lon=lon,
            is_remote=is_remote,
            employment_type=employment_type,
            commitment=(
                "Full Time"
                if full_time is True
                else "Part Time" if full_time is False else None
            ),
            department=(
                department.strip()
                if isinstance(department, str) and department.strip()
                else None
            ),
            requisition_id=(
                requisition.strip()
                if isinstance(requisition, str) and requisition.strip()
                else None
            ),
            posted_at=_parse_date(item.get("PostedDate")),
            fetched_at=datetime.now(UTC),
            raw={
                "featured": item.get("Featured"),
                "job_location_type": location_type,
                "opportunity_type": item.get("OpportunityType"),
            },
        )


def _normalize_board_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("UKG board URL cannot be empty")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or _HOST_RE.fullmatch(host) is None
        or len(segments) < 3
        or _TENANT_RE.fullmatch(segments[0]) is None
        or segments[1].casefold() != "jobboard"
        or _UUID_RE.fullmatch(segments[2]) is None
    ):
        raise ValueError(f"Invalid UKG board URL: {value!r}")
    return f"https://{host}/{segments[0]}/JobBoard/{segments[2]}"


def _search_payload(skip: int) -> dict[str, object]:
    return {
        "Top": PAGE_SIZE,
        "Skip": skip,
        "QueryString": "",
        "OrderBy": [
            {
                "Value": "postedDateDesc",
                "PropertyName": "PostedDate",
                "Ascending": False,
            }
        ],
        "OrderByKey": None,
        "Filters": [],
        "Coordinates": None,
        "Extent": None,
        "ProximitySearchType": 0,
    }


def _extract_detail_payload(html_text: str) -> dict[str, Any]:
    start = html_text.find(_DETAIL_MARKER)
    if start < 0:
        raise ScraperError("UKG detail page omitted opportunity data")
    start += len(_DETAIL_MARKER)
    try:
        payload, _ = json.JSONDecoder().raw_decode(html_text[start:])
    except json.JSONDecodeError as exc:
        raise ScraperError(f"UKG detail data was invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScraperError("UKG detail data was not an object")
    return payload


def _apply_detail(job: Job, detail: dict[str, Any]) -> None:
    description = detail.get("Description")
    if isinstance(description, str) and description.strip():
        job.description = _html_to_text(description)[:25_000] or None
    if updated := _parse_date(detail.get("UpdatedDate")):
        raw = dict(job.raw or {})
        raw["updated_at"] = updated.isoformat()
        job.raw = raw
    location, country_iso, lat, lon = _locations(detail.get("Locations"))
    if location:
        job.location = location
    if country_iso:
        job.country_iso = country_iso
    if lat is not None and lon is not None:
        job.lat = lat
        job.lon = lon
    if detail.get("JobLocationType") == 2:
        job.is_remote = True
    _apply_compensation(job, detail)


def _locations(
    value: object,
) -> tuple[str | None, str | None, float | None, float | None]:
    if not isinstance(value, list):
        return None, None, None, None
    rendered: list[str] = []
    country_codes: set[str] = set()
    coordinates: list[tuple[float, float]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        address = entry.get("Address")
        address = address if isinstance(address, dict) else {}
        parts: list[str] = []
        localized = entry.get("LocalizedName")
        if isinstance(localized, str) and localized.strip():
            parts.append(localized.strip())
        for key in ("City", "State", "Country"):
            item = address.get(key)
            if isinstance(item, dict):
                item = item.get("Code") if key == "State" else item.get("Name")
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        location = ", ".join(_dedupe_strings(parts))
        if location and location.casefold() not in {
            existing.casefold() for existing in rendered
        }:
            rendered.append(location)

        country = address.get("Country")
        if isinstance(country, dict):
            code = country.get("Code")
            if isinstance(code, str):
                normalized = _normalize_country_code(code)
                if normalized:
                    country_codes.add(normalized)
        point = entry.get("Coordinates")
        if isinstance(point, dict):
            latitude = _to_float(point.get("Latitude"))
            longitude = _to_float(point.get("Longitude"))
            if latitude is not None and longitude is not None:
                coordinates.append((latitude, longitude))

    country_iso = next(iter(country_codes)) if len(country_codes) == 1 else None
    lat = lon = None
    if len(coordinates) == 1:
        lat, lon = coordinates[0]
    return "; ".join(rendered) or None, country_iso, lat, lon


def _apply_compensation(job: Job, detail: dict[str, Any]) -> None:
    annual_min = _to_float(detail.get("CompensationAnnualMinimum"))
    annual_max = _to_float(detail.get("CompensationAnnualMaximum"))
    hourly_min = _to_float(detail.get("CompensationHourlyMinimum"))
    hourly_max = _to_float(detail.get("CompensationHourlyMaximum"))
    period = None
    minimum = maximum = None
    if annual_min is not None or annual_max is not None:
        period, minimum, maximum = "YEAR", annual_min, annual_max
    elif hourly_min is not None or hourly_max is not None:
        period, minimum, maximum = "HOUR", hourly_min, hourly_max
    else:
        pay_range = detail.get("PayRange")
        if isinstance(pay_range, dict):
            minimum = _to_float(pay_range.get("PayRangeMinimum"))
            maximum = _to_float(pay_range.get("PayRangeMaximum"))
    currency = (
        detail.get("CompensationCurrencyCode")
        or detail.get("PayRangeCurrencyCode")
    )
    if minimum is not None:
        job.salary_min = minimum
    if maximum is not None:
        job.salary_max = maximum
    if isinstance(currency, str) and len(currency.strip()) == 3:
        job.salary_currency = currency.strip().upper()
    if period is not None:
        job.salary_period = period


def _required_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScraperError(f"UKG job omitted {key}")
    return value.strip()


def _normalize_country_code(value: str) -> str | None:
    code = value.strip().upper()
    if len(code) == 2:
        return code
    return _COUNTRY_CODES.get(code)


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _html_to_text(value: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ScraperError(
            "UKG scraper requires beautifulsoup4; "
            "install `ats-scrapers[scrapers]`"
        ) from exc
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(text)).strip()


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    with contextlib.suppress(TypeError, ValueError):
        return float(value)  # type: ignore[arg-type]
    return None
