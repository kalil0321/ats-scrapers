"""Softgarden public career feed scraper.

Current Softgarden career sites expose every active posting as a complete
Schema.org ``DataFeed``:

    GET https://{tenant}.career.softgarden.de/jobs.feed.json

The credential-free feed includes descriptions, employer identity, dates,
employment type, canonical job URLs, and structured locations. No browser,
API key, or per-job detail requests are required.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers._slug import require_host_label, require_http_url
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

FEED_URL = "https://{tenant}.career.softgarden.de/jobs.feed.json"
HOST_SUFFIX = ".career.softgarden.de"
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "CONTRACT": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}
_COUNTRY_CODES = {
    "angola": "AO",
    "argentina": "AR",
    "austria": "AT",
    "belgien": "BE",
    "belgium": "BE",
    "bosnien und herzegowina": "BA",
    "bulgarien": "BG",
    "croatia": "HR",
    "dänemark": "DK",
    "deutschland": "DE",
    "egypt": "EG",
    "españa": "ES",
    "france": "FR",
    "frankreich": "FR",
    "germany": "DE",
    "greece": "GR",
    "hong kong": "HK",
    "hungary": "HU",
    "ireland": "IE",
    "italien": "IT",
    "italy": "IT",
    "kroatien": "HR",
    "luxemburg": "LU",
    "malaysia": "MY",
    "marokko": "MA",
    "niederlande": "NL",
    "nigeria": "NG",
    "nordmazedonien": "MK",
    "north macedonia": "MK",
    "norway": "NO",
    "österreich": "AT",
    "poland": "PL",
    "polen": "PL",
    "polska": "PL",
    "portugal": "PT",
    "qatar": "QA",
    "romania": "RO",
    "rumänien": "RO",
    "saudi arabia": "SA",
    "schweden": "SE",
    "schweiz": "CH",
    "singapore": "SG",
    "slovenia": "SI",
    "south africa": "ZA",
    "south korea": "KR",
    "spanien": "ES",
    "spain": "ES",
    "südafrika": "ZA",
    "suisse": "CH",
    "sweden": "SE",
    "tansania": "TZ",
    "thailand": "TH",
    "tschechien": "CZ",
    "ukraine": "UA",
    "united kingdom": "GB",
    "united states": "US",
    "usa": "US",
    "vereinigtes königreich": "GB",
}
_EUROPE_CODES = frozenset(
    {
        "AT",
        "BA",
        "BE",
        "BG",
        "CH",
        "CZ",
        "DE",
        "DK",
        "ES",
        "FR",
        "GB",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LU",
        "MK",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "UA",
    }
)
_ASIA_CODES = frozenset({"HK", "KR", "MY", "QA", "SA", "SG", "TH"})
_AFRICA_CODES = frozenset({"AO", "EG", "MA", "NG", "TZ", "ZA"})
_REMOTE_RE = re.compile(r"\b(?:homeoffice|remote|remote work|mobiles arbeiten)\b", re.I)


@ScraperRegistry.register(ATSType.SOFTGARDEN)
class SoftgardenScraper(BaseScraper):
    """Scrape one public Softgarden career feed."""

    ats = ATSType.SOFTGARDEN
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
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        self.company_slug = _normalize_tenant(company_slug)
        self.feed_url = FEED_URL.format(tenant=self.company_slug)

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            payload = await fetch.get_json(self.feed_url)
        return self._parse_feed(payload)

    def _parse_feed(self, payload: object) -> list[Job]:
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Softgarden ({self.company_slug}) feed was not an object"
            )
        elements = payload.get("dataFeedElement")
        expected_total = payload.get("numberOfItems")
        if not isinstance(elements, list) or not isinstance(expected_total, int):
            raise ScraperError(
                f"Softgarden ({self.company_slug}) feed omitted its jobs/count"
            )
        if expected_total != len(elements):
            raise ScraperError(
                f"Softgarden ({self.company_slug}) expected {expected_total} jobs "
                f"but received {len(elements)}"
            )

        jobs: list[Job] = []
        seen: set[str] = set()
        for index, element in enumerate(elements):
            if not isinstance(element, dict) or not isinstance(
                element.get("item"), dict
            ):
                raise ScraperError(
                    f"Softgarden ({self.company_slug}) row {index} "
                    "was not a JobPosting"
                )
            job = self._parse_job(element["item"], element.get("dateModified"))
            if job.ats_id in seen:
                raise ScraperError(
                    f"Softgarden ({self.company_slug}) returned duplicate "
                    f"job ID {job.ats_id}"
                )
            seen.add(job.ats_id or "")
            jobs.append(job)
        return jobs

    def _parse_job(
        self,
        item: dict[str, Any],
        date_modified: object,
    ) -> Job:
        identifier = item.get("identifier")
        organization = item.get("hiringOrganization")
        if not isinstance(identifier, dict) or not isinstance(organization, dict):
            raise ScraperError(
                f"Softgarden ({self.company_slug}) job omitted identity metadata"
            )
        job_id = _job_id(identifier.get("value"))
        title = _required_string(item, "title")
        company = _required_string(organization, "name")
        job_url = _trusted_job_url(item.get("url"), job_id)
        description = _string(item.get("description"))
        location, country_iso = _locations(item.get("jobLocation"))
        employment_raw = _string(item.get("employmentType"))
        employment_type = _employment_type(employment_raw)
        remote_text = " ".join(
            part for part in (title, location) if isinstance(part, str)
        )
        raw = {
            key: value
            for key, value in {
                "feed_modified_at": date_modified,
                "industry": organization.get("industry"),
                "organization_url": organization.get("url"),
            }.items()
            if value not in (None, "")
        }
        return Job(
            url=job_url,
            title=title,
            company=company,
            ats_type=ATSType.SOFTGARDEN,
            ats_id=job_id,
            location=location,
            country_iso=country_iso,
            region=_region(country_iso),
            is_remote=True if _REMOTE_RE.search(remote_text) else None,
            employment_type=employment_type,
            commitment=employment_raw,
            description=(
                description[:25_000]
                if self.include_descriptions and description
                else None
            ),
            posted_at=_parse_datetime(item.get("datePosted")),
            fetched_at=datetime.now(UTC),
            apply_url=job_url,
            raw=raw or None,
        )


def _normalize_tenant(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if cleaned.startswith(("http://", "https://")):
        try:
            parsed = urlparse(cleaned)
            port = parsed.port
        except ValueError as exc:
            raise ScraperError(
                "Softgarden URL must use a public career.softgarden.de host"
            ) from exc
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host.endswith(HOST_SUFFIX)
            or host == HOST_SUFFIX.removeprefix(".")
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise ScraperError(
                "Softgarden URL must use a public career.softgarden.de host"
            )
        cleaned = host.removesuffix(HOST_SUFFIX)
    elif cleaned.lower().endswith(HOST_SUFFIX):
        cleaned = cleaned[: -len(HOST_SUFFIX)]
    return require_host_label(cleaned.lower(), provider="SoftgardenScraper")


def _trusted_job_url(value: object, job_id: str) -> str:
    url = _string(value)
    if not url:
        raise ScraperError("Softgarden job omitted its canonical URL")
    trusted_url = require_http_url(url, provider="SoftgardenScraper")
    try:
        parsed = urlparse(trusted_url)
        port = parsed.port
    except ValueError as exc:
        raise ScraperError("Softgarden job returned an untrusted URL") from exc
    segments = [segment for segment in parsed.path.split("/") if segment]
    valid_job_path = (
        len(segments) >= 2
        and segments[0] in {"job", "jobs"}
        and segments[1] == job_id
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not valid_job_path
    ):
        raise ScraperError("Softgarden job returned an untrusted URL")
    return parsed._replace(query="", fragment="").geturl()


def _locations(value: object) -> tuple[str | None, str | None]:
    raw_locations = value if isinstance(value, list) else [value]
    rendered: list[str] = []
    country_codes: list[str | None] = []
    for location in raw_locations:
        if not isinstance(location, dict) or not isinstance(
            location.get("address"), dict
        ):
            continue
        address = location["address"]
        country_raw = _string(address.get("addressCountry"))
        country_iso = _country_code(country_raw)
        locality = " ".join(
            part
            for part in (
                _string(address.get("postalCode")),
                _string(address.get("addressLocality")),
            )
            if part
        )
        parts = [
            part
            for part in (
                _string(address.get("streetAddress")),
                locality or None,
                _string(address.get("addressRegion")),
                country_raw if country_raw != "-" else None,
            )
            if part
        ]
        if parts:
            rendered.append(", ".join(dict.fromkeys(parts)))
            country_codes.append(country_iso)
    resolved_codes = {code for code in country_codes if code}
    country_iso = (
        next(iter(resolved_codes))
        if country_codes
        and all(country_codes)
        and len(resolved_codes) == 1
        else None
    )
    return "; ".join(dict.fromkeys(rendered)) or None, country_iso


def _country_code(value: str | None) -> str | None:
    if not value or value == "-":
        return None
    upper = value.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return _COUNTRY_CODES.get(value.casefold())


def _region(country_iso: str | None) -> str | None:
    if country_iso in _EUROPE_CODES:
        return "Europe"
    if country_iso in _ASIA_CODES:
        return "Asia"
    if country_iso in _AFRICA_CODES:
        return "Africa"
    if country_iso == "US":
        return "North America"
    if country_iso in {"AR"}:
        return "South America"
    return None


def _employment_type(value: str | None) -> EmploymentType | None:
    if not value:
        return None
    normalized = re.sub(r"[\s-]+", "_", value.upper())
    return _EMPLOYMENT_TYPE_MAP.get(normalized)


def _parse_datetime(value: object) -> datetime | None:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _job_id(value: object) -> str:
    if isinstance(value, bool):
        raise ScraperError("Softgarden job ID must be a positive integer")
    text = str(value).strip() if isinstance(value, int | str) else ""
    if not text.isdigit() or int(text) <= 0:
        raise ScraperError("Softgarden job ID must be a positive integer")
    return text


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = _string(payload.get(key))
    if not value:
        raise ScraperError(f"Softgarden job omitted required field {key!r}")
    return value


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
