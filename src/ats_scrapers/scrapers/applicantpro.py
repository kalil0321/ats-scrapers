"""ApplicantPro public career-site scraper."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.fetch import MalformedJSONError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher


CAREERS_URL = "https://{tenant}.applicantpro.com/jobs/"
LISTING_API_URL = "https://{tenant}.applicantpro.com/core/jobs/{domain_id}"
DETAIL_API_URL = (
    "https://{tenant}.applicantpro.com/core/jobs/{domain_id}/{job_id}/job-details"
)
DETAIL_CONCURRENCY = 8
_DOMAIN_ID_RE = re.compile(r"\bdomainId\s*:\s*([0-9]+)")
_TITLE_PREFIX_RE = re.compile(r"^Job Listings\s*-\s*", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(r"\s+Jobs\s*$", re.IGNORECASE)
_EMPLOYMENT_PATTERNS: tuple[tuple[str, EmploymentType], ...] = (
    ("intern", "INTERN"),
    ("part time", "PART_TIME"),
    ("part-time", "PART_TIME"),
    ("full time", "FULL_TIME"),
    ("full-time", "FULL_TIME"),
    ("contract", "CONTRACT"),
    ("temporary", "TEMPORARY"),
)
_ISO3_TO_ISO2 = {
    "ARE": "AE",
    "ARG": "AR",
    "AUS": "AU",
    "AUT": "AT",
    "BEL": "BE",
    "BRA": "BR",
    "CAN": "CA",
    "CHE": "CH",
    "CHL": "CL",
    "CHN": "CN",
    "COL": "CO",
    "COD": "CD",
    "CRI": "CR",
    "CUB": "CU",
    "CYM": "KY",
    "CZE": "CZ",
    "DEU": "DE",
    "DNK": "DK",
    "ESP": "ES",
    "EGY": "EG",
    "FIN": "FI",
    "FRA": "FR",
    "GBR": "GB",
    "GRC": "GR",
    "GUM": "GU",
    "HKG": "HK",
    "HUN": "HU",
    "IDN": "ID",
    "IND": "IN",
    "IRL": "IE",
    "ISR": "IL",
    "ITA": "IT",
    "JPN": "JP",
    "KOR": "KR",
    "MEX": "MX",
    "MYS": "MY",
    "NLD": "NL",
    "NGA": "NG",
    "NOR": "NO",
    "NZL": "NZ",
    "NPL": "NP",
    "PAK": "PK",
    "PHL": "PH",
    "POL": "PL",
    "PRT": "PT",
    "ROU": "RO",
    "SAU": "SA",
    "SGP": "SG",
    "SWE": "SE",
    "THA": "TH",
    "TCA": "TC",
    "TUR": "TR",
    "TWN": "TW",
    "USA": "US",
    "VNM": "VN",
    "ZAF": "ZA",
    "ATA": "AQ",
    "ECU": "EC",
}
_EUROPE_CODES = {
    "AT",
    "BE",
    "CH",
    "CZ",
    "DE",
    "DK",
    "ES",
    "FI",
    "FR",
    "GB",
    "GR",
    "HU",
    "IE",
    "IT",
    "NL",
    "NO",
    "PL",
    "PT",
    "RO",
    "SE",
}
_ASIA_CODES = {
    "AE",
    "CN",
    "HK",
    "ID",
    "IL",
    "IN",
    "JP",
    "KR",
    "MY",
    "NP",
    "PAK",
    "PH",
    "SA",
    "SG",
    "TH",
    "TR",
    "TW",
    "VN",
}
_SOUTH_AMERICA_CODES = {"AR", "BR", "CL", "CO", "EC"}
_CURRENCY_BY_COUNTRY = {
    "AE": "AED",
    "AR": "ARS",
    "AU": "AUD",
    "AT": "EUR",
    "BE": "EUR",
    "BR": "BRL",
    "CA": "CAD",
    "CH": "CHF",
    "CL": "CLP",
    "CN": "CNY",
    "CO": "COP",
    "CR": "CRC",
    "CU": "CUP",
    "CZ": "CZK",
    "DE": "EUR",
    "DK": "DKK",
    "EC": "USD",
    "EG": "EGP",
    "ES": "EUR",
    "FI": "EUR",
    "FR": "EUR",
    "GB": "GBP",
    "GR": "EUR",
    "GU": "USD",
    "HK": "HKD",
    "HU": "HUF",
    "ID": "IDR",
    "IE": "EUR",
    "IL": "ILS",
    "IN": "INR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "KY": "KYD",
    "MX": "MXN",
    "MY": "MYR",
    "NG": "NGN",
    "NL": "EUR",
    "NO": "NOK",
    "NZ": "NZD",
    "NP": "NPR",
    "PH": "PHP",
    "PK": "PKR",
    "PL": "PLN",
    "PT": "EUR",
    "RO": "RON",
    "SA": "SAR",
    "SE": "SEK",
    "SG": "SGD",
    "TC": "USD",
    "TH": "THB",
    "TR": "TRY",
    "TW": "TWD",
    "US": "USD",
    "VN": "VND",
    "ZA": "ZAR",
}


@ScraperRegistry.register(ATSType.APPLICANTPRO)
class ApplicantProScraper(BaseScraper):
    """Scrape one public ApplicantPro employer portal."""

    ats = ATSType.APPLICANTPRO
    default_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json,text/html,application/xhtml+xml",
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
        self.company_slug = require_host_label(
            company_slug,
            provider="ApplicantProScraper",
        ).casefold()
        self.company_name = (
            company_name.strip()
            if isinstance(company_name, str) and company_name.strip()
            else None
        )

    async def afetch(self) -> list[Job]:
        careers_url = CAREERS_URL.format(tenant=self.company_slug)
        async with self.make_fetcher() as fetch:
            page = await fetch.get_text(careers_url)
            domain_id, page_company = _portal_identity(page, self.company_slug)
            payload = await fetch.get_json(
                LISTING_API_URL.format(
                    tenant=self.company_slug,
                    domain_id=domain_id,
                ),
                params={"getParams": json.dumps({}, separators=(",", ":"))},
            )
            jobs = self._parse_listing(
                payload,
                domain_id=domain_id,
                company=self.company_name or page_company,
            )
            if self.include_descriptions and jobs:
                semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
                resolved = await asyncio.gather(
                    *(
                        self._enrich_detail(fetch, semaphore, domain_id, job)
                        for job in jobs
                    )
                )
                jobs = [
                    job
                    for job, detail_exists in zip(jobs, resolved, strict=True)
                    if detail_exists
                ]
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        domain_id = str((job.raw or {}).get("domain_id") or "")
        if not domain_id:
            return None
        copy = job.model_copy(deep=True)

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                semaphore = asyncio.Semaphore(1)
                await self._enrich_detail(fetch, semaphore, domain_id, copy)
            return copy.description

        return self._run_sync(run())

    def _parse_listing(
        self,
        payload: Any,
        *,
        domain_id: str,
        company: str,
    ) -> list[Job]:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) returned an unsuccessful listing"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) returned malformed listing data"
            )
        items = data.get("jobs")
        if not isinstance(items, list):
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) returned no jobs list"
            )
        expected = data.get("jobCount")
        if not isinstance(expected, int) or expected != len(items):
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) expected {expected!r} jobs, "
                f"received {len(items)}"
            )

        jobs: list[Job] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ScraperError(
                    f"ApplicantPro ({self.company_slug}) returned a malformed job"
                )
            job = self._parse_job(item, domain_id=domain_id, company=company)
            if job.ats_id in seen:
                raise ScraperError(
                    f"ApplicantPro ({self.company_slug}) returned duplicate "
                    f"job id {job.ats_id!r}"
                )
            seen.add(str(job.ats_id))
            jobs.append(job)
        return jobs

    def _parse_job(
        self,
        item: dict[str, Any],
        *,
        domain_id: str,
        company: str,
    ) -> Job:
        job_id = _required_text(item.get("id"), "job id", self.company_slug)
        title = _required_text(item.get("title"), "title", self.company_slug)
        country_iso = _country_iso(item.get("iso3"))
        location = _location(item)
        commitment = _clean_text(item.get("employmentType"))
        salary_min = _amount(item.get("minSalary"))
        salary_max = _amount(item.get("maxSalary"))
        salary_period = _salary_period(
            item.get("payTypeFrame"),
            item.get("payType"),
        )
        salary_summary = _salary_summary(item)
        salary_currency = (
            _CURRENCY_BY_COUNTRY.get(country_iso or "")
            if salary_min is not None or salary_max is not None
            else None
        )
        if salary_currency is None:
            salary_min = None
            salary_max = None
            salary_period = None
        workplace = _clean_text(item.get("workplaceType"))
        department = _clean_text(item.get("orgTitle"))
        team = _clean_text(item.get("parentTitle"))
        posted_at = _parse_date(item.get("startDateRef"))
        url = f"https://{self.company_slug}.applicantpro.com/jobs/{job_id}"
        raw = {
            "domain_id": domain_id,
            "site_id": item.get("siteId"),
            "country_iso3": _clean_text(item.get("iso3")),
            "end_date": _clean_text(item.get("endDateRef")),
            "until_filled": item.get("untilFilled"),
            "classification": _clean_text(item.get("classification")),
            "workplace_type": workplace,
            "pay_details": _clean_text(item.get("payDetails")),
        }
        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.APPLICANTPRO,
            ats_id=f"{self.company_slug}:{job_id}",
            location=location,
            country_iso=country_iso,
            region=_region(country_iso),
            is_remote=_is_remote(workplace),
            salary_currency=salary_currency,
            salary_period=salary_period,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=_employment_type(commitment),
            department=department,
            team=team,
            requisition_id=job_id,
            apply_url=url,
            commitment=commitment,
            posted_at=posted_at,
            raw=raw,
        )

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        domain_id: str,
        job: Job,
    ) -> bool:
        job_id = (job.requisition_id or "").strip()
        if not job_id:
            return False
        url = DETAIL_API_URL.format(
            tenant=self.company_slug,
            domain_id=domain_id,
            job_id=job_id,
        )
        async with semaphore:
            try:
                payload = await fetch.get_json(url)
            except CompanyNotFoundError:
                return False
            except MalformedJSONError:
                raise
            except ScraperError:
                return True
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) returned an "
                f"unsuccessful detail for {job_id!r}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) returned malformed "
                f"detail data for {job_id!r}"
            )
        detail_id = _clean_text(data.get("id"))
        if detail_id != job_id:
            raise ScraperError(
                f"ApplicantPro ({self.company_slug}) detail id "
                f"{detail_id!r} did not match {job_id!r}"
            )
        description = _html_description(
            data.get("advertisingDescriptionHtml")
            or data.get("description")
            or data.get("advertisingDescription")
        )
        if description:
            job.description = description[:25_000]
        posted_at = _parse_date(data.get("startDateRef"))
        if posted_at:
            job.posted_at = posted_at
        if job.raw is not None:
            benefits = _clean_text(data.get("benefits"))
            if benefits:
                job.raw["benefits"] = benefits
            job.raw["hide_from_indeed"] = data.get("hideFromIndeed")
        return True


def _portal_identity(page: str, tenant: str) -> tuple[str, str]:
    domain_match = _DOMAIN_ID_RE.search(page)
    if domain_match is None:
        raise CompanyNotFoundError(
            f"ApplicantPro tenant has no public careers site: {tenant}"
        )
    soup = BeautifulSoup(page, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    company = _TITLE_SUFFIX_RE.sub("", _TITLE_PREFIX_RE.sub("", title)).strip()
    return domain_match.group(1), company or tenant


def _required_text(value: object, field: str, tenant: str) -> str:
    cleaned = _clean_text(value)
    if cleaned:
        return cleaned
    raise ScraperError(f"ApplicantPro ({tenant}) job has no {field}")


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _html_description(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _parse_date(value: object) -> datetime | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    for date_format in ("%b %d, %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, date_format).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _country_iso(value: object) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    upper = cleaned.upper()
    if len(upper) == 2:
        return upper
    return _ISO3_TO_ISO2.get(upper)


def _location(item: dict[str, Any]) -> str | None:
    explicit = _clean_text(item.get("jobLocation"))
    if explicit:
        return explicit
    return ", ".join(
        value
        for value in (
            _clean_text(item.get("streetAddress")),
            _clean_text(item.get("city")),
            _clean_text(item.get("abbreviation")),
            _clean_text(item.get("iso3")),
        )
        if value
    ) or None


def _employment_type(value: str | None) -> EmploymentType | None:
    normalized = (value or "").replace("_", " ").casefold()
    return next(
        (mapped for marker, mapped in _EMPLOYMENT_PATTERNS if marker in normalized),
        None,
    )


def _is_remote(value: str | None) -> bool | None:
    normalized = (value or "").casefold()
    if "remote" in normalized:
        return True
    if "onsite" in normalized or "on-site" in normalized:
        return False
    return None


def _salary_period(frame: object, pay_type: object) -> SalaryPeriod | None:
    normalized = f"{_clean_text(frame) or ''} {_clean_text(pay_type) or ''}".casefold()
    for marker, period in (
        ("hour", "HOUR"),
        ("day", "DAY"),
        ("week", "WEEK"),
        ("month", "MONTH"),
        ("year", "YEAR"),
        ("salary", "YEAR"),
    ):
        if marker in normalized:
            return period
    return None


def _amount(value: object) -> float | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    match = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", cleaned)
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _salary_summary(item: dict[str, Any]) -> str | None:
    minimum = _clean_text(item.get("minSalary"))
    maximum = _clean_text(item.get("maxSalary"))
    frame = _clean_text(item.get("payTypeFrame"))
    details = _clean_text(item.get("payDetails"))
    base = f"{minimum} - {maximum}" if minimum and maximum else minimum or maximum or ""
    if frame and base:
        base = f"{base} {frame}".strip()
    if details and details.casefold() not in {"doe", "depending on experience"}:
        base = f"{base} ({details})".strip()
    return base or details


def _region(country_iso: str | None) -> str | None:
    if country_iso in {"US", "CA", "MX"}:
        return "North America"
    if country_iso in {"CR", "CU", "KY", "TC"}:
        return "North America"
    if country_iso in _EUROPE_CODES:
        return "Europe"
    if country_iso in _ASIA_CODES:
        return "Asia"
    if country_iso in _SOUTH_AMERICA_CODES:
        return "South America"
    if country_iso in {"AU", "GU", "NZ"}:
        return "Oceania"
    if country_iso in {"CD", "EG", "NG", "ZA"}:
        return "Africa"
    if country_iso == "AQ":
        return "Antarctica"
    return None
