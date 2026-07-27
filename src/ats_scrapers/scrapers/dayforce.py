"""Dayforce public job-feed scraper."""

from __future__ import annotations

import contextlib
import html
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import HttpUrl, ValidationError

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

API_ROOT = "https://www.dayforcehcm.com/api"
CAREERS_HOST = "jobs.dayforcehcm.com"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z]{2})?$")

_EMPLOYMENT_TYPE_MAP: tuple[tuple[str, EmploymentType], ...] = (
    ("full time", "FULL_TIME"),
    ("full-time", "FULL_TIME"),
    ("part time", "PART_TIME"),
    ("part-time", "PART_TIME"),
    ("contractor", "CONTRACT"),
    ("contract", "CONTRACT"),
    ("fixed term", "CONTRACT"),
    ("fixed-term", "CONTRACT"),
    ("temporary", "TEMPORARY"),
    ("seasonal", "TEMPORARY"),
    ("casual", "TEMPORARY"),
    ("internship", "INTERN"),
    ("intern", "INTERN"),
    ("apprentice", "INTERN"),
)

_COUNTRY_CODES: tuple[tuple[str, str, str], ...] = (
    ("AR", "ARG", "South America"),
    ("AU", "AUS", "Oceania"),
    ("AT", "AUT", "Europe"),
    ("BE", "BEL", "Europe"),
    ("BR", "BRA", "South America"),
    ("CA", "CAN", "North America"),
    ("CH", "CHE", "Europe"),
    ("CL", "CHL", "South America"),
    ("CN", "CHN", "Asia"),
    ("CO", "COL", "South America"),
    ("CZ", "CZE", "Europe"),
    ("DE", "DEU", "Europe"),
    ("DK", "DNK", "Europe"),
    ("ES", "ESP", "Europe"),
    ("FI", "FIN", "Europe"),
    ("FR", "FRA", "Europe"),
    ("GB", "GBR", "Europe"),
    ("HK", "HKG", "Asia"),
    ("IN", "IND", "Asia"),
    ("IE", "IRL", "Europe"),
    ("IT", "ITA", "Europe"),
    ("JP", "JPN", "Asia"),
    ("KR", "KOR", "Asia"),
    ("MX", "MEX", "North America"),
    ("MY", "MYS", "Asia"),
    ("NL", "NLD", "Europe"),
    ("NO", "NOR", "Europe"),
    ("NZ", "NZL", "Oceania"),
    ("PL", "POL", "Europe"),
    ("PT", "PRT", "Europe"),
    ("SG", "SGP", "Asia"),
    ("SE", "SWE", "Europe"),
    ("TH", "THA", "Asia"),
    ("US", "USA", "North America"),
    ("ZA", "ZAF", "Africa"),
)
_COUNTRY_METADATA: dict[str, tuple[str, str]] = {
    code: (iso2, region)
    for iso2, iso3, region in _COUNTRY_CODES
    for code in (iso2, iso3)
}


@ScraperRegistry.register(ATSType.DAYFORCE)
class DayforceScraper(BaseScraper):
    """Scrape one public Dayforce career-site feed without credentials."""

    ats = ATSType.DAYFORCE
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
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
        self.tenant, self.board = _normalize_tenant_board(company_slug)
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        api_url = f"{API_ROOT}/{self.tenant}/V1/JobFeeds"
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_json(
                api_url,
                params={
                    "includeActivePostingOnly": "true",
                    "internalJobBoardCode": self.board,
                },
            )
        if not isinstance(payload, list):
            raise ScraperError(
                f"Dayforce ({self.tenant}/{self.board}) returned a non-list feed"
            )

        variants_by_id: dict[str, list[tuple[Job, dict[str, Any]]]] = {}
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ScraperError(
                    f"Dayforce feed row {index} was not an object"
                )
            job = self._parse_job(item)
            if not job.ats_id:
                raise ScraperError(f"Dayforce feed row {index} omitted its job id")
            variants_by_id.setdefault(job.ats_id, []).append((job, item))
        return [
            _select_job_variant(ats_id, variants)
            for ats_id, variants in variants_by_id.items()
        ]

    def _parse_job(self, item: dict[str, Any]) -> Job:
        reference = _required_string(item, "ReferenceNumber")
        title = _required_string(item, "Title")
        ats_id = f"{self.tenant}:{self.board}:{reference}"
        details_url = _validated_details_url(
            item.get("JobDetailsUrl"),
            tenant=self.tenant,
            board=self.board,
            reference=reference,
            culture=_string(item.get("CultureCode")),
        )
        company = (
            self.company_name
            or _string(item.get("CompanyName"))
            or _string(item.get("ParentCompanyName"))
            or self.tenant
        )
        country = _string(item.get("Country"))
        country_metadata = _COUNTRY_METADATA.get(country.upper()) if country else None
        location = _location(item)
        virtual = item.get("IsVirtualLocation")
        is_remote = (
            virtual
            if isinstance(virtual, bool)
            else (True if location and "remote" in location.lower() else None)
        )
        employment_label = _string(item.get("EmploymentIndicator"))
        description = (
            _clean_description(item.get("Description"))
            if self.include_descriptions
            else None
        )
        raw = {
            key: value
            for key in (
                "ClientSiteName",
                "ClientSiteXRefCode",
                "CultureCode",
                "ParentCompanyName",
                "ParentRequisitionCode",
                "LastUpdated",
                "JobType",
                "TravelPercentage",
                "TravelRequired",
                "Education",
                "PostalCode",
            )
            if (value := item.get(key)) not in (None, "", [], {})
        }

        return Job(
            url=details_url,
            title=title,
            company=company,
            ats_type=ATSType.DAYFORCE,
            ats_id=ats_id,
            location=location,
            country_iso=country_metadata[0] if country_metadata else None,
            region=country_metadata[1] if country_metadata else None,
            is_remote=is_remote,
            employment_type=_employment_type(employment_label),
            department=_string(item.get("JobFamily")),
            team=_string(item.get("JobFunction")),
            description=description,
            posted_at=_parse_date(item.get("DatePosted")),
            fetched_at=datetime.now(tz=UTC),
            language=_language(item.get("CultureCode")),
            requisition_id=reference,
            apply_url=_validated_apply_url(
                item.get("ApplyUrl"),
                tenant=self.tenant,
                board=self.board,
                reference=reference,
            ),
            commitment=employment_label,
            raw=raw or None,
        )


def _select_job_variant(
    ats_id: str,
    variants: list[tuple[Job, dict[str, Any]]],
) -> Job:
    cultures_to_titles: dict[str, set[str]] = {}
    companies: set[str] = set()
    for job, item in variants:
        culture = (_string(item.get("CultureCode")) or "").casefold()
        cultures_to_titles.setdefault(culture, set()).add(
            " ".join(job.title.split()).casefold()
        )
        feed_company = (
            _string(item.get("CompanyName"))
            or _string(item.get("ParentCompanyName"))
            or ""
        )
        companies.add(" ".join(feed_company.split()).casefold())

    if len(companies) != 1 or any(
        len(titles) != 1 for titles in cultures_to_titles.values()
    ):
        raise ScraperError(
            f"Dayforce returned conflicting duplicate job id {ats_id!r}"
        )

    selected_job, _ = max(
        variants,
        key=lambda variant: _variant_rank(*variant),
    )
    cultures = sorted(
        {
            culture
            for _, item in variants
            if (culture := _string(item.get("CultureCode")))
        }
    )
    locations = list(
        dict.fromkeys(
            job.location for job, _ in variants if job.location
        )
    )
    raw = dict(selected_job.raw or {})
    if len(cultures) > 1:
        raw["AvailableCultures"] = cultures
    if len(locations) > 1:
        raw["AllLocations"] = locations
    selected_job.raw = raw or None
    return selected_job


def _variant_rank(job: Job, item: dict[str, Any]) -> tuple[bool, bool, bool, bool]:
    culture = (_string(item.get("CultureCode")) or "").casefold()
    country = (_string(item.get("Country")) or "").upper()
    return (
        bool(job.description),
        culture == "en-us",
        culture.startswith("en-"),
        country in _COUNTRY_METADATA,
    )


def _normalize_tenant_board(value: str) -> tuple[str, str]:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Dayforce tenant/board cannot be empty")
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != CAREERS_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Dayforce URL must use https://jobs.dayforcehcm.com"
            )
        segments = [segment for segment in parsed.path.split("/") if segment]
    else:
        segments = [segment for segment in raw.split("/") if segment]

    def parse_parts(parts: list[str]) -> tuple[str, str] | None:
        if len(parts) < 2:
            return None
        tenant, board = parts[:2]
        if (
            not _SEGMENT_RE.fullmatch(tenant)
            or not _SEGMENT_RE.fullmatch(board)
        ):
            return None
        remainder = [segment.casefold() for segment in parts[2:]]
        if remainder and not (
            remainder[0] == "jobs"
            and len(remainder) in {1, 2, 3}
            and (len(remainder) == 1 or remainder[1].isdigit())
            and (len(remainder) < 3 or remainder[2] == "apply")
        ):
            return None
        return tenant, board

    if (tenant_board := parse_parts(segments)) is not None:
        return tenant_board
    if (
        len(segments) >= 3
        and _LOCALE_RE.fullmatch(segments[0])
        and (tenant_board := parse_parts(segments[1:])) is not None
    ):
        return tenant_board
    raise ValueError(f"Invalid Dayforce tenant/board: {value!r}")


def _validated_details_url(
    value: object,
    *,
    tenant: str,
    board: str,
    reference: str,
    culture: str | None,
) -> HttpUrl:
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
        try:
            parsed = urlparse(candidate)
            segments = [segment for segment in parsed.path.split("/") if segment]
            if len(segments) == 5 and _LOCALE_RE.fullmatch(segments[0]):
                segments.pop(0)
            if (
                parsed.scheme != "https"
                or parsed.hostname != CAREERS_HOST
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or len(segments) != 4
                or segments[0].casefold() != tenant.casefold()
                or segments[1].casefold() != board.casefold()
                or segments[2].casefold() != "jobs"
                or segments[3] != reference
            ):
                raise ScraperError(
                    f"Dayforce ({tenant}/{board}) returned an unsafe job URL"
                )
            return HttpUrl(candidate)
        except (ValidationError, ValueError) as exc:
            raise ScraperError(
                f"Dayforce ({tenant}/{board}) returned an unsafe job URL"
            ) from exc
    locale = culture if culture and _LOCALE_RE.fullmatch(culture) else "en-US"
    return HttpUrl(
        f"https://{CAREERS_HOST}/{locale}/{tenant}/{board}/jobs/{reference}"
    )


def _validated_apply_url(
    value: object,
    *,
    tenant: str,
    board: str,
    reference: str,
) -> HttpUrl | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) == 6 and _LOCALE_RE.fullmatch(segments[0]):
            segments.pop(0)
        if (
            parsed.scheme != "https"
            or parsed.hostname != CAREERS_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or len(segments) != 5
            or segments[0].casefold() != tenant.casefold()
            or segments[1].casefold() != board.casefold()
            or segments[2].casefold() != "jobs"
            or segments[3] != reference
            or segments[4].casefold() != "apply"
        ):
            return None
        return HttpUrl(candidate)
    except (ValidationError, ValueError):
        return None


def _location(item: dict[str, Any]) -> str | None:
    parts = [
        value
        for value in (
            _string(item.get("AddressLine1")),
            _string(item.get("City")),
            _string(item.get("State")),
            _string(item.get("Country")),
        )
        if value
    ]
    return ", ".join(dict.fromkeys(parts)) or None


def _employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    normalized = value.casefold().replace("_", " ")
    for marker, mapped in _EMPLOYMENT_TYPE_MAP:
        if marker in normalized:
            return mapped
    return None


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return html.unescape(value).strip()[:25_000] or None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _language(value: object) -> str | None:
    culture = _string(value)
    if not culture:
        return None
    language = culture.split("-", 1)[0].lower()
    return language if len(language) == 2 and language.isalpha() else None


def _required_string(item: dict[str, Any], key: str) -> str:
    value = _string(item.get(key))
    if not value:
        raise ScraperError(f"Dayforce job omitted {key}")
    return value


def _string(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        normalized = str(value).strip()
        return normalized or None
    return None
