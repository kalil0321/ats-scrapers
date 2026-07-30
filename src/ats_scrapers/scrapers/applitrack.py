"""Frontline AppliTrack public job-posting scraper.

AppliTrack hosts credential-free employer portals at:

    https://www.applitrack.com/{tenant}/onlineapp/

The all-postings endpoint returns JavaScript containing server-rendered job
markup, including full descriptions. This scraper parses the inert string
content directly and never executes the JavaScript.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.applitrack\.com$",
    re.IGNORECASE,
)
_TENANT_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)
_POSTING_RE = re.compile(
    r"<ul class=\\?['\"]postingsList\\?['\"] "
    r"id=\\?['\"]p(?P<job>\d+)_(?P<district>[^\\'\"\s>]*)",
    re.IGNORECASE,
)
_ADVERTISED_RE = re.compile(
    r"Viewing All Types(?:&nbsp;|\s)*\(<b>([\d,]+)</b>\s+openings?\)",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"<td id=\\?['\"]wrapword\\?['\"][^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)
_BOARD_RE = re.compile(
    r"found at (.*?)\. The position is",
    re.IGNORECASE | re.DOTALL,
)
_COMPENSATION_RE = re.compile(
    r"Salary Range:</td>.*?Full/Part-Time:</td>.*?"
    r"Work Days/Year:</td>.*?</tr><tr>"
    r"<td><span[^>]*>(?P<salary>.*?)</span></td>"
    r"<td><span[^>]*>(?P<commitment>.*?)</span></td>"
    r"<td><span[^>]*>(?P<work_days>.*?)</span></td>",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENT_BOUNDARY_RE = re.compile(
    r"'\);\s*document\.write\('",
    re.IGNORECASE,
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_SPACE_RE = re.compile(r"[ \t\f\v]+")
_EMPTY_MARKERS = (
    "&nbsp;(no results)",
    "We currently do not have any vacant positions",
)
_REGION_BY_COUNTRY = {
    "CA": "North America",
    "US": "North America",
}


@ScraperRegistry.register(ATSType.APPLITRACK)
class AppliTrackScraper(BaseScraper):
    """Scrape one public Frontline AppliTrack tenant."""

    ats = ATSType.APPLITRACK
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "text/javascript,text/html;q=0.9,*/*;q=0.1",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 120.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        company_name: str | None = None,
        country_iso: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.host, self.tenant = _normalize_tenant(company_slug)
        self.company_slug = (
            f"https://{self.host}/{self.tenant}/onlineapp"
        )
        self.company_name = _string(company_name)
        self.country_iso = _country_iso(country_iso)
        self.listing_url = (
            f"{self.company_slug}/JobPostings/Output.asp?all=1"
        )

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_text(self.listing_url)
        return self._parse_payload(payload)

    def _parse_payload(self, payload: str) -> list[Job]:
        if "AppliTrackOutput" not in payload:
            raise ScraperError(
                f"AppliTrack ({self.tenant}) response omitted "
                "AppliTrackOutput"
            )
        advertised_match = _ADVERTISED_RE.search(payload)
        advertised = (
            int(advertised_match.group(1).replace(",", ""))
            if advertised_match
            else None
        )
        matches = list(_POSTING_RE.finditer(payload))
        if not matches:
            if advertised == 0 or any(
                marker in payload for marker in _EMPTY_MARKERS
            ):
                return []
            raise ScraperError(
                f"AppliTrack ({self.tenant}) response contained no "
                "recognized postings or empty-board marker"
            )
        if advertised is None:
            raise ScraperError(
                f"AppliTrack ({self.tenant}) response omitted its "
                "advertised opening count"
            )
        if advertised != len(matches):
            raise ScraperError(
                f"AppliTrack ({self.tenant}) advertised {advertised} "
                f"openings but exposed {len(matches)} posting blocks"
            )
        jobs_by_id: dict[str, Job] = {}
        for index, match in enumerate(matches):
            end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(payload)
            )
            job = self._parse_block(
                payload[match.start():end],
                match.group("job"),
                match.group("district"),
            )
            ats_id = job.ats_id or ""
            existing = jobs_by_id.get(ats_id)
            if existing is not None:
                jobs_by_id[ats_id] = _merge_duplicate_job(
                    existing,
                    job,
                    self.tenant,
                )
                continue
            jobs_by_id[ats_id] = job
        return list(jobs_by_id.values())

    def _parse_block(
        self,
        block: str,
        job_id: str,
        district_id: str,
    ) -> Job:
        title_match = _TITLE_RE.search(block)
        title = (
            _clean_fragment(title_match.group(1))
            if title_match
            else None
        )
        if title is None:
            raise ScraperError(
                f"AppliTrack ({self.tenant}) posting {job_id} "
                "omitted its title"
            )
        district = _field(block, "District")
        board_match = _BOARD_RE.search(block)
        board = (
            _clean_fragment(board_match.group(1))
            if board_match
            else None
        )
        company = (
            district
            or board
            or self.company_name
            or self.tenant
        )
        position_type = _field(block, "Position Type")
        location = _field(block, "Location")
        posted = _field(block, "Date Posted")
        salary_summary, commitment, work_days = _compensation(block)
        employment_type = _employment_type(commitment)
        posting_code = (
            f"{job_id}_{district_id}" if district_id else job_id
        )
        ats_id = (
            f"{district_id}:{job_id}"
            if district_id
            else f"{self.tenant}:{job_id}"
        )
        job_url = (
            f"{self.company_slug}/JobPostings/view.asp?"
            f"AppliTrackJobId={posting_code}"
            "&AppliTrackLayoutMode=detail"
            "&AppliTrackViewPosting=1"
        )
        description = _description(block, job_id, district_id)
        raw = {
            key: value
            for key, value in {
                "source_tenant": self.tenant,
                "district_id": district_id or None,
                "position_type": position_type,
                "date_available": _field(block, "Date Available"),
                "closing_date": _field(block, "Closing Date"),
                "work_days_per_year": work_days,
            }.items()
            if value not in (None, "")
        }
        return Job(
            url=job_url,
            title=title,
            company=company,
            ats_type=ATSType.APPLITRACK,
            ats_id=ats_id,
            location=location,
            country_iso=self.country_iso,
            region=_REGION_BY_COUNTRY.get(self.country_iso or ""),
            salary_summary=salary_summary,
            employment_type=employment_type,
            department=position_type,
            description=(
                description[:25_000]
                if self.include_descriptions and description
                else None
            ),
            posted_at=_parse_date(posted),
            fetched_at=datetime.now(UTC),
            apply_url=job_url,
            commitment=commitment,
            raw=raw or None,
        )


def _normalize_tenant(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    if not cleaned:
        raise ScraperError("AppliTrackScraper requires a tenant or URL")
    if "://" not in cleaned:
        tenant = cleaned.casefold()
        if not _TENANT_RE.fullmatch(tenant):
            raise ScraperError(
                f"AppliTrackScraper rejected invalid tenant: {value!r}"
            )
        return "www.applitrack.com", tenant
    parsed = urlparse(cleaned)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ScraperError(
            f"AppliTrackScraper rejected untrusted URL: {value!r}"
        )
    try:
        if parsed.port is not None:
            raise ScraperError(
                f"AppliTrackScraper rejected untrusted URL: {value!r}"
            )
    except ValueError as error:
        raise ScraperError(
            f"AppliTrackScraper rejected untrusted URL: {value!r}"
        ) from error
    host = (parsed.hostname or "").casefold()
    if host == "applitrack.com":
        host = "www.applitrack.com"
    if not _HOST_RE.fullmatch(host):
        raise ScraperError(
            f"AppliTrackScraper rejected untrusted URL: {value!r}"
        )
    segments = [
        segment for segment in parsed.path.split("/") if segment
    ]
    if (
        len(segments) < 2
        or segments[1].casefold() != "onlineapp"
        or not _TENANT_RE.fullmatch(segments[0])
    ):
        raise ScraperError(
            f"AppliTrackScraper rejected untrusted URL: {value!r}"
        )
    return host, segments[0].casefold()


def _merge_duplicate_job(
    existing: Job,
    duplicate: Job,
    tenant: str,
) -> Job:
    excluded = {"fetched_at", "department", "raw"}
    if (
        existing.model_dump(exclude=excluded)
        != duplicate.model_dump(exclude=excluded)
    ):
        raise ScraperError(
            f"AppliTrack ({tenant}) returned conflicting duplicate "
            f"posting ID {existing.ats_id}"
        )

    existing_raw = dict(existing.raw or {})
    duplicate_raw = dict(duplicate.raw or {})
    existing_position = existing_raw.pop("position_type", None)
    duplicate_position = duplicate_raw.pop("position_type", None)
    if existing_raw != duplicate_raw:
        raise ScraperError(
            f"AppliTrack ({tenant}) returned conflicting duplicate "
            f"posting metadata for ID {existing.ats_id}"
        )

    positions = list(
        dict.fromkeys(
            value
            for value in (existing_position, duplicate_position)
            if value
        )
    )
    merged_raw = existing_raw
    if positions:
        merged_raw["position_type"] = positions[0]
    if len(positions) > 1:
        merged_raw["position_types"] = positions
    return existing.model_copy(
        update={
            "department": positions[0] if positions else None,
            "raw": merged_raw or None,
        }
    )


def _field(block: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}:\s*</span><br\s*/?>.*?"
        r"<span[^>]*>(.*?)</span>",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    return _clean_fragment(match.group(1)) if match else None


def _description(
    block: str,
    job_id: str,
    district_id: str,
) -> str | None:
    marker = re.escape(f"DescriptionText{job_id}_{district_id}")
    match = re.search(
        rf"id=\\?['\"]{marker}\\?['\"][^>]*>(.*?)"
        r"<br\s*/?><img[^>]+clear\.gif",
        block,
        re.IGNORECASE | re.DOTALL,
    )
    return _clean_fragment(match.group(1)) if match else None


def _compensation(
    block: str,
) -> tuple[str | None, str | None, str | None]:
    match = _COMPENSATION_RE.search(block)
    if match is None:
        return None, None, None
    return (
        _clean_fragment(match.group("salary")),
        _clean_fragment(match.group("commitment")),
        _clean_fragment(match.group("work_days")),
    )


def _clean_fragment(value: str | None) -> str | None:
    if not value:
        return None
    value = _DOCUMENT_BOUNDARY_RE.sub("", value)
    value = _UNICODE_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    value = _HEX_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    value = (
        value.replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\r", "\n")
        .replace("\\n", "\n")
        .replace("\\t", " ")
        .replace("\\\\", "\\")
    )
    text = BeautifulSoup(
        html.unescape(value),
        "html.parser",
    ).get_text("\n", strip=True)
    text = "\n".join(
        _SPACE_RE.sub(" ", line).strip()
        for line in text.splitlines()
        if line.strip()
    )
    return text or None


def _parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def _employment_type(value: str | None) -> EmploymentType | None:
    normalized = (value or "").casefold()
    if "part" in normalized and "time" in normalized:
        return "PART_TIME"
    if "full" in normalized and "time" in normalized:
        return "FULL_TIME"
    return None


def _country_iso(value: str | None) -> str | None:
    cleaned = _string(value)
    if cleaned is None:
        return None
    candidate = cleaned.upper()
    if not re.fullmatch(r"[A-Z]{2}", candidate):
        raise ScraperError(
            f"AppliTrackScraper rejected invalid country code: {value!r}"
        )
    return candidate


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
