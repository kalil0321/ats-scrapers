"""Paycom public career portal scraper.

Public Paycom employer portals bootstrap a short-lived session JWT in their
HTML, then use credential-free JSON endpoints for active listings and details:

    GET  https://www.paycomonline.net/v4/ats/web.php/portal/{token}/career-page
    POST https://*.paycomonline.net/api/ats/job-posting-previews/search
    GET  https://*.paycomonline.net/api/ats/job-postings/{job_id}

The detail payload includes complete descriptions, structured Google Jobs
metadata, locations, salary, dates, employer identity, and classification.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

PORTAL_URL = (
    "https://www.paycomonline.net/v4/ats/web.php/portal/{token}/career-page"
)
JOB_URL = "https://www.paycomonline.net/v4/ats/web.php/portal/{token}/jobs/{job_id}"
SEARCH_PATH = "/api/ats/job-posting-previews/search"
DETAIL_PATH = "/api/ats/job-postings/{job_id}"
PAGE_SIZE = 100
DETAIL_CONCURRENCY = 6
_DETAIL_HANDLED = frozenset({404, 410})
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.I)
_CONFIG_RE = re.compile(
    r"var configsFromHost = (\{.*?\});\s*var Mountable",
    re.S,
)
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "FULLTIME": "FULL_TIME",
    "FULL TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "PARTTIME": "PART_TIME",
    "PART TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "SEASONAL": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}
_SALARY_PERIOD_MAP: dict[str, SalaryPeriod] = {
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
_COUNTRY_CODES = {
    "CAN": "CA",
    "CANADA": "CA",
    "MEX": "MX",
    "MEXICO": "MX",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "USA": "US",
}
_REGION_BY_COUNTRY = {
    "CA": "North America",
    "MX": "North America",
    "US": "North America",
}
logger = logging.getLogger(__name__)


@ScraperRegistry.register(ATSType.PAYCOM)
class PaycomScraper(BaseScraper):
    """Scrape one public Paycom career portal token."""

    ats = ATSType.PAYCOM
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "text/html,application/xhtml+xml,application/json",
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
        self.portal_token = _normalize_portal_token(company_slug)
        self.company_slug = self.portal_token
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )
        self.portal_url = PORTAL_URL.format(token=self.portal_token)

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            service_url, api_headers, client_code = await self._bootstrap(fetch)
            previews = await self._fetch_previews(
                fetch,
                service_url,
                api_headers,
            )
            jobs = [
                self._parse_preview(preview, client_code)
                for preview in previews
            ]
            if not self.include_descriptions or not jobs:
                return jobs

            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            enriched = await asyncio.gather(
                *(
                    self._enrich_detail(
                        fetch,
                        semaphore,
                        service_url,
                        api_headers,
                        job,
                    )
                    for job in jobs
                )
            )
        completed = [job for job in enriched if job is not None]
        if jobs and not completed:
            raise ScraperError(
                f"Paycom ({self.portal_token}) lost every listed job "
                "during detail validation"
            )
        return completed

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                service_url, api_headers, _ = await self._bootstrap(fetch)
                enriched = await self._enrich_detail(
                    fetch,
                    asyncio.Semaphore(1),
                    service_url,
                    api_headers,
                    copy,
                )
            return enriched.description if enriched is not None else None

        return self._run_sync(run())

    async def _bootstrap(
        self,
        fetch: Fetcher,
    ) -> tuple[str, dict[str, str], str]:
        html_text = await fetch.get_text(self.portal_url)
        config = _extract_config(html_text)
        session_jwt = _required_string(config, "sessionJWT")
        claims = _decode_jwt_claims(session_jwt)
        client_code = _required_string(claims, "clientcode")
        lib_config_raw = _required_string(config, "libConfig")
        try:
            lib_config = json.loads(lib_config_raw)
        except json.JSONDecodeError as exc:
            raise ScraperError(
                f"Paycom ({self.portal_token}) returned invalid library config"
            ) from exc
        if not isinstance(lib_config, dict):
            raise ScraperError(
                f"Paycom ({self.portal_token}) library config was not an object"
            )
        service_url = _trusted_service_url(
            _required_string(lib_config, "atsPortalMantleServiceUrl")
        )
        locale = _string(lib_config.get("locale")) or "en-US"
        highlights = str(bool(lib_config.get("translationHighlights"))).lower()
        return (
            service_url,
            {
                "Authorization": session_jwt,
                "Locale": locale,
                "Translation-Highlights": highlights,
                "Portal-Host-Referrer": self.portal_url,
            },
            client_code,
        )

    async def _fetch_previews(
        self,
        fetch: Fetcher,
        service_url: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        expected_total: int | None = None
        skip = 0
        while expected_total is None or skip < expected_total:
            response = await fetch.request(
                "POST",
                f"{service_url}{SEARCH_PATH}",
                headers=headers,
                json=_search_payload(skip),
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ScraperError(
                    f"Paycom ({self.portal_token}) search response was not an object"
                )
            if expected_total is None:
                raw_total = payload.get("jobPostingPreviewsCount")
                if not isinstance(raw_total, int) or raw_total < 0:
                    raise ScraperError(
                        f"Paycom ({self.portal_token}) search omitted a valid count"
                    )
                expected_total = raw_total
            page = payload.get("jobPostingPreviews")
            if not isinstance(page, list):
                raise ScraperError(
                    f"Paycom ({self.portal_token}) search omitted the jobs list"
                )
            if not page:
                break
            for index, item in enumerate(page):
                if not isinstance(item, dict):
                    raise ScraperError(
                        f"Paycom ({self.portal_token}) row {skip + index} "
                        "was not an object"
                    )
                previews.append(item)
            skip += len(page)
        if expected_total != len(previews):
            raise ScraperError(
                f"Paycom ({self.portal_token}) expected {expected_total} jobs "
                f"but received {len(previews)}"
            )
        ids = [_job_id(item.get("jobId")) for item in previews]
        if len(ids) != len(set(ids)):
            raise ScraperError(
                f"Paycom ({self.portal_token}) returned duplicate job IDs"
            )
        return previews

    def _parse_preview(
        self,
        preview: dict[str, Any],
        client_code: str,
    ) -> Job:
        job_id = _job_id(preview.get("jobId"))
        title = _required_string(preview, "jobTitle")
        location = _string(preview.get("locations"))
        remote_type = _string(preview.get("remoteType"))
        position_type = _string(preview.get("positionType"))
        description = _string(preview.get("description"))
        job_url = JOB_URL.format(
            token=self.portal_token,
            job_id=job_id,
        )
        raw = {
            key: value
            for key, value in {
                "client_code": client_code,
                "portal_token": self.portal_token,
                "remote_type": remote_type,
                "is_hot_job": preview.get("isHotJob"),
            }.items()
            if value not in (None, "")
        }
        return Job(
            url=job_url,
            title=title,
            company=self.company_name or client_code,
            ats_type=ATSType.PAYCOM,
            ats_id=f"{client_code}:{job_id}",
            location=location,
            is_remote=_is_remote(remote_type, location),
            employment_type=_employment_type(position_type),
            commitment=position_type,
            apply_url=job_url,
            description=(
                description[:25_000]
                if self.include_descriptions and description
                else None
            ),
            posted_at=_parse_datetime(preview.get("postedOn")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        service_url: str,
        headers: dict[str, str],
        job: Job,
    ) -> Job | None:
        job_id = (job.ats_id or "").rsplit(":", 1)[-1]
        async with semaphore:
            try:
                response = await fetch.request(
                    "GET",
                    f"{service_url}{DETAIL_PATH.format(job_id=job_id)}",
                    headers=headers,
                    handled=_DETAIL_HANDLED,
                )
            except ScraperError as exc:
                logger.warning(
                    "Retaining Paycom job %s without detail metadata after "
                    "detail failure: %s",
                    job.ats_id,
                    exc,
                )
                return job
        if response.status_code in _DETAIL_HANDLED:
            return None
        try:
            payload = response.json()
            _apply_detail(job, payload)
        except (ScraperError, ValueError) as exc:
            logger.warning(
                "Retaining Paycom job %s without detail metadata after "
                "detail parsing failure: %s",
                job.ats_id,
                exc,
            )
        return job


def _normalize_portal_token(value: str) -> str:
    raw = value.strip().rstrip("/")
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError as exc:
            raise ScraperError(
                "Paycom URL must be an HTTPS public career portal URL"
            ) from exc
        segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"paycomonline.net", "www.paycomonline.net"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or len(segments) < 5
            or segments[:4] != ["v4", "ats", "web.php", "portal"]
        ):
            raise ScraperError(
                "Paycom URL must be an HTTPS public career portal URL"
            )
        raw = segments[4]
    if not _TOKEN_RE.fullmatch(raw):
        raise ScraperError(
            f"PaycomScraper: invalid portal token {value!r}; "
            "expected exactly 32 hexadecimal characters"
        )
    return raw.lower()


def _extract_config(html_text: str) -> dict[str, Any]:
    match = _CONFIG_RE.search(html_text)
    if match is None:
        raise ScraperError("Paycom page omitted the public portal configuration")
    try:
        config = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ScraperError("Paycom portal configuration was invalid JSON") from exc
    if not isinstance(config, dict):
        raise ScraperError("Paycom portal configuration was not an object")
    return config


def _decode_jwt_claims(session_jwt: str) -> dict[str, Any]:
    parts = session_jwt.split(".")
    if len(parts) != 3:
        raise ScraperError("Paycom portal session token was malformed")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(encoded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScraperError("Paycom portal session claims were invalid") from exc
    if not isinstance(claims, dict):
        raise ScraperError("Paycom portal session claims were not an object")
    return claims


def _trusted_service_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ScraperError(
            "Paycom portal returned an untrusted API service URL"
        ) from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or not (host == "paycomonline.net" or host.endswith(".paycomonline.net"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise ScraperError("Paycom portal returned an untrusted API service URL")
    return value.rstrip("/")


def _search_payload(skip: int) -> dict[str, Any]:
    return {
        "skip": skip,
        "take": PAGE_SIZE,
        "filtersForQuery": {
            "distanceFrom": 0,
            "workEnvironments": [],
            "positionTypes": [],
            "educationLevels": [],
            "categories": [],
            "travelTypes": [],
            "shiftTypes": [],
            "otherFilters": [],
            "keywordSearchText": "",
            "location": "",
            "sortOption": "",
        },
    }


def _apply_detail(job: Job, payload: object) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobPosting"), dict):
        raise ScraperError(f"Paycom detail for {job.ats_id} omitted jobPosting")
    detail = payload["jobPosting"]
    try:
        google_job = _google_job_payload(detail.get("googleJobJson"))
    except ScraperError as exc:
        logger.warning(
            "Ignoring malformed optional Google Jobs metadata for Paycom "
            "job %s: %s",
            job.ats_id,
            exc,
        )
        google_job = {}

    title = _string(detail.get("jobTitle")) or _string(google_job.get("title"))
    if title:
        job.title = title
    organization = google_job.get("hiringOrganization")
    if isinstance(organization, dict):
        company = _string(organization.get("name"))
        if company:
            job.company = company

    description = _detail_description(detail)
    if description and (
        not job.description or len(description) > len(job.description)
    ):
        job.description = description[:25_000]

    location = _detail_location(detail)
    if location:
        job.location = location
    country_iso = _google_country(google_job)
    if country_iso:
        job.country_iso = country_iso
        job.region = _REGION_BY_COUNTRY.get(country_iso)

    remote_type = _string(detail.get("remoteType"))
    remote = _is_remote(remote_type, job.location)
    if remote is not None:
        job.is_remote = remote

    salary_summary = _string(detail.get("salaryRange"))
    if salary_summary:
        job.salary_summary = salary_summary
    _apply_google_salary(job, google_job.get("baseSalary"), salary_summary)

    posted_at = _parse_datetime(google_job.get("datePosted"))
    if posted_at:
        job.posted_at = posted_at
    position_type = _string(detail.get("positionType"))
    google_employment = _string(google_job.get("employmentType"))
    commitment = position_type or google_employment
    if commitment:
        job.commitment = commitment
        job.employment_type = (
            _employment_type(position_type)
            or _employment_type(google_employment)
        )
    category = _string(detail.get("jobCategory"))
    if category:
        job.department = category
    identifier = _string(google_job.get("identifier"))
    if identifier:
        job.requisition_id = identifier

    raw = dict(job.raw or {})
    raw.update(
        {
            key: value
            for key, value in {
                "client_code": detail.get("clientCode"),
                "level": detail.get("level"),
                "job_shift": detail.get("jobShift"),
                "education_level": detail.get("educationLevel"),
                "travel_percentage": detail.get("travelPercentage"),
                "is_hot_job": detail.get("isHotJob"),
            }.items()
            if value not in (None, "")
        }
    )
    job.raw = raw or None


def _detail_description(detail: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for body_key, title_key in (
        ("description", "descriptionTitle"),
        ("qualifications", "qualificationsTitle"),
    ):
        body = _string(detail.get(body_key))
        if not body:
            continue
        title = _string(detail.get(title_key))
        parts.append(
            f"<h2>{html.escape(title)}</h2>{body}"
            if title
            else body
        )
    return "\n".join(parts) or None


def _detail_location(detail: dict[str, Any]) -> str | None:
    locations: list[str] = []
    primary = _string(detail.get("location"))
    if primary:
        locations.append(primary)
    secondary = detail.get("secondaryLocations")
    if isinstance(secondary, list):
        for item in secondary:
            value = (
                _string(item)
                if isinstance(item, str)
                else _string(item.get("location"))
                if isinstance(item, dict)
                else None
            )
            if value:
                locations.append(value)
    return "; ".join(dict.fromkeys(locations)) or None


def _google_job_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ScraperError("Paycom Google Jobs payload was invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ScraperError("Paycom Google Jobs payload was not an object")
    return payload


def _google_country(google_job: dict[str, Any]) -> str | None:
    location = google_job.get("jobLocation")
    locations = location if isinstance(location, list) else [location]
    countries: set[str] = set()
    for item in locations:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if not isinstance(address, dict):
            continue
        raw = _string(address.get("addressCountry"))
        if not raw:
            continue
        upper = raw.upper()
        country = upper if len(upper) == 2 else _COUNTRY_CODES.get(upper)
        if country:
            countries.add(country)
    return next(iter(countries)) if len(countries) == 1 else None


def _apply_google_salary(
    job: Job,
    value: object,
    salary_summary: str | None,
) -> None:
    if not isinstance(value, dict):
        return
    currency = _string(value.get("currency"))
    amount = value.get("value")
    if not currency or len(currency) != 3 or not isinstance(amount, dict):
        return
    job.salary_currency = currency.upper()
    minimum = amount.get("minValue")
    maximum = amount.get("maxValue")
    exact = amount.get("value")
    if (
        not isinstance(exact, bool)
        and isinstance(exact, int | float)
    ):
        minimum = exact if minimum is None else minimum
        maximum = exact if maximum is None else maximum
    if not isinstance(minimum, bool) and isinstance(minimum, int | float):
        job.salary_min = float(minimum)
    if not isinstance(maximum, bool) and isinstance(maximum, int | float):
        job.salary_max = float(maximum)
    unit = _string(amount.get("unitText"))
    job.salary_period = _salary_period(unit or salary_summary)


def _employment_type(value: object) -> EmploymentType | None:
    text = _string(value)
    if not text:
        return None
    normalized = re.sub(r"[_\-\s]+", " ", text.upper()).strip()
    for key, mapped in _EMPLOYMENT_TYPE_MAP.items():
        key_normalized = re.sub(r"[_\-\s]+", " ", key).strip()
        if normalized == key_normalized or key_normalized in normalized:
            return mapped
    return None


def _salary_period(value: object) -> SalaryPeriod | None:
    text = _string(value)
    if not text:
        return None
    upper = text.upper()
    for key, period in _SALARY_PERIOD_MAP.items():
        if key in upper:
            return period
    return None


def _is_remote(remote_type: object, location: object) -> bool | None:
    remote_text = _string(remote_type)
    location_text = _string(location)
    if remote_text and "remote" in remote_text.casefold():
        return True
    if location_text and "remote" in location_text.casefold():
        return True
    return None


def _parse_datetime(value: object) -> datetime | None:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _job_id(value: object) -> int:
    if isinstance(value, bool):
        raise ScraperError("Paycom job ID must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScraperError("Paycom job ID must be a positive integer") from exc
    if parsed <= 0:
        raise ScraperError("Paycom job ID must be a positive integer")
    return parsed


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = _string(payload.get(key))
    if not value:
        raise ScraperError(f"Paycom payload omitted required field {key!r}")
    return value


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
