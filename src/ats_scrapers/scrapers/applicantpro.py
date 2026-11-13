"""ApplicantPro public career-site scraper."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job, SalaryPeriod
from ats_scrapers.scrapers._bs4 import bs4_soup
from ats_scrapers.scrapers._htmltree import parse_html
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
    "ABW": "AW", "AFG": "AF", "AGO": "AO", "AIA": "AI", "ALA": "AX",
    "ALB": "AL", "AND": "AD", "ARE": "AE", "ARG": "AR", "ARM": "AM",
    "ASM": "AS", "ATA": "AQ", "ATF": "TF", "ATG": "AG", "AUS": "AU",
    "AUT": "AT", "AZE": "AZ", "BDI": "BI", "BEL": "BE", "BEN": "BJ",
    "BES": "BQ", "BFA": "BF", "BGD": "BD", "BGR": "BG", "BHR": "BH",
    "BHS": "BS", "BIH": "BA", "BLM": "BL", "BLR": "BY", "BLZ": "BZ",
    "BMU": "BM", "BOL": "BO", "BRA": "BR", "BRB": "BB", "BRN": "BN",
    "BTN": "BT", "BVT": "BV", "BWA": "BW", "CAF": "CF", "CAN": "CA",
    "CCK": "CC", "CHE": "CH", "CHL": "CL", "CHN": "CN", "CIV": "CI",
    "CMR": "CM", "COD": "CD", "COG": "CG", "COK": "CK", "COL": "CO",
    "COM": "KM", "CPV": "CV", "CRI": "CR", "CUB": "CU", "CUW": "CW",
    "CXR": "CX", "CYM": "KY", "CYP": "CY", "CZE": "CZ", "DEU": "DE",
    "DJI": "DJ", "DMA": "DM", "DNK": "DK", "DOM": "DO", "DZA": "DZ",
    "ECU": "EC", "EGY": "EG", "ERI": "ER", "ESH": "EH", "ESP": "ES",
    "EST": "EE", "ETH": "ET", "FIN": "FI", "FJI": "FJ", "FLK": "FK",
    "FRA": "FR", "FRO": "FO", "FSM": "FM", "GAB": "GA", "GBR": "GB",
    "GEO": "GE", "GGY": "GG", "GHA": "GH", "GIB": "GI", "GIN": "GN",
    "GLP": "GP", "GMB": "GM", "GNB": "GW", "GNQ": "GQ", "GRC": "GR",
    "GRD": "GD", "GRL": "GL", "GTM": "GT", "GUF": "GF", "GUM": "GU",
    "GUY": "GY", "HKG": "HK", "HMD": "HM", "HND": "HN", "HRV": "HR",
    "HTI": "HT", "HUN": "HU", "IDN": "ID", "IMN": "IM", "IND": "IN",
    "IOT": "IO", "IRL": "IE", "IRN": "IR", "IRQ": "IQ", "ISL": "IS",
    "ISR": "IL", "ITA": "IT", "JAM": "JM", "JEY": "JE", "JOR": "JO",
    "JPN": "JP", "KAZ": "KZ", "KEN": "KE", "KGZ": "KG", "KHM": "KH",
    "KIR": "KI", "KNA": "KN", "KOR": "KR", "KWT": "KW", "LAO": "LA",
    "LBN": "LB", "LBR": "LR", "LBY": "LY", "LCA": "LC", "LIE": "LI",
    "LKA": "LK", "LSO": "LS", "LTU": "LT", "LUX": "LU", "LVA": "LV",
    "MAC": "MO", "MAF": "MF", "MAR": "MA", "MCO": "MC", "MDA": "MD",
    "MDG": "MG", "MDV": "MV", "MEX": "MX", "MHL": "MH", "MKD": "MK",
    "MLI": "ML", "MLT": "MT", "MMR": "MM", "MNE": "ME", "MNG": "MN",
    "MNP": "MP", "MOZ": "MZ", "MRT": "MR", "MSR": "MS", "MTQ": "MQ",
    "MUS": "MU", "MWI": "MW", "MYS": "MY", "MYT": "YT", "NAM": "NA",
    "NCL": "NC", "NER": "NE", "NFK": "NF", "NGA": "NG", "NIC": "NI",
    "NIU": "NU", "NLD": "NL", "NOR": "NO", "NPL": "NP", "NRU": "NR",
    "NZL": "NZ", "OMN": "OM", "PAK": "PK", "PAN": "PA", "PCN": "PN",
    "PER": "PE", "PHL": "PH", "PLW": "PW", "PNG": "PG", "POL": "PL",
    "PRI": "PR", "PRK": "KP", "PRT": "PT", "PRY": "PY", "PSE": "PS",
    "PYF": "PF", "QAT": "QA", "REU": "RE", "ROU": "RO", "RUS": "RU",
    "RWA": "RW", "SAU": "SA", "SDN": "SD", "SEN": "SN", "SGP": "SG",
    "SGS": "GS", "SHN": "SH", "SJM": "SJ", "SLB": "SB", "SLE": "SL",
    "SLV": "SV", "SMR": "SM", "SOM": "SO", "SPM": "PM", "SRB": "RS",
    "SSD": "SS", "STP": "ST", "SUR": "SR", "SVK": "SK", "SVN": "SI",
    "SWE": "SE", "SWZ": "SZ", "SXM": "SX", "SYC": "SC", "SYR": "SY",
    "TCA": "TC", "TCD": "TD", "TGO": "TG", "THA": "TH", "TJK": "TJ",
    "TKL": "TK", "TKM": "TM", "TLS": "TL", "TON": "TO", "TTO": "TT",
    "TUN": "TN", "TUR": "TR", "TUV": "TV", "TWN": "TW", "TZA": "TZ",
    "UGA": "UG", "UKR": "UA", "UMI": "UM", "URY": "UY", "USA": "US",
    "UZB": "UZ", "VAT": "VA", "VCT": "VC", "VEN": "VE", "VGB": "VG",
    "VIR": "VI", "VNM": "VN", "VUT": "VU", "WLF": "WF", "WSM": "WS",
    "YEM": "YE", "ZAF": "ZA", "ZMB": "ZM", "ZWE": "ZW",
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
    "IS",
    "IT",
    "NL",
    "NO",
    "PL",
    "PT",
    "RO",
    "SE",
    "UA",
}
_ASIA_CODES = {
    "AE",
    "CN",
    "HK",
    "ID",
    "IL",
    "IN",
    "JP",
    "JO",
    "KR",
    "LB",
    "MY",
    "NP",
    "PH",
    "PK",
    "SA",
    "SG",
    "TH",
    "TR",
    "TW",
    "VN",
}
_SOUTH_AMERICA_CODES = {"AR", "BO", "BR", "CL", "CO", "EC"}
# The currency in use in each country, from Unicode CLDR (territory
# currencies in tender on 2026-11-13). Countries with several currencies in
# tender (Bhutan, Haiti, Lesotho, Namibia, Panama, Palestine, Zimbabwe) and
# Antarctica are left out: the currency of an amount cannot be told there.
_CURRENCY_BY_COUNTRY = {
    "AD": "EUR", "AE": "AED", "AF": "AFN", "AG": "XCD", "AI": "XCD",
    "AL": "ALL", "AM": "AMD", "AO": "AOA", "AR": "ARS", "AS": "USD",
    "AT": "EUR", "AU": "AUD", "AW": "AWG", "AX": "EUR", "AZ": "AZN",
    "BA": "BAM", "BB": "BBD", "BD": "BDT", "BE": "EUR", "BF": "XOF",
    "BG": "BGN", "BH": "BHD", "BI": "BIF", "BJ": "XOF", "BL": "EUR",
    "BM": "BMD", "BN": "BND", "BO": "BOB", "BQ": "USD", "BR": "BRL",
    "BS": "BSD", "BV": "NOK", "BW": "BWP", "BY": "BYN", "BZ": "BZD",
    "CA": "CAD", "CC": "AUD", "CD": "CDF", "CF": "XAF", "CG": "XAF",
    "CH": "CHF", "CI": "XOF", "CK": "NZD", "CL": "CLP", "CM": "XAF",
    "CN": "CNY", "CO": "COP", "CR": "CRC", "CU": "CUP", "CV": "CVE",
    "CW": "XCG", "CX": "AUD", "CY": "EUR", "CZ": "CZK", "DE": "EUR",
    "DJ": "DJF", "DK": "DKK", "DM": "XCD", "DO": "DOP", "DZ": "DZD",
    "EC": "USD", "EE": "EUR", "EG": "EGP", "EH": "MAD", "ER": "ERN",
    "ES": "EUR", "ET": "ETB", "FI": "EUR", "FJ": "FJD", "FK": "FKP",
    "FM": "USD", "FO": "DKK", "FR": "EUR", "GA": "XAF", "GB": "GBP",
    "GD": "XCD", "GE": "GEL", "GF": "EUR", "GG": "GBP", "GH": "GHS",
    "GI": "GIP", "GL": "DKK", "GM": "GMD", "GN": "GNF", "GP": "EUR",
    "GQ": "XAF", "GR": "EUR", "GS": "GBP", "GT": "GTQ", "GU": "USD",
    "GW": "XOF", "GY": "GYD", "HK": "HKD", "HM": "AUD", "HN": "HNL",
    "HR": "EUR", "HU": "HUF", "ID": "IDR", "IE": "EUR", "IL": "ILS",
    "IM": "GBP", "IN": "INR", "IO": "USD", "IQ": "IQD", "IR": "IRR",
    "IS": "ISK", "IT": "EUR", "JE": "GBP", "JM": "JMD", "JO": "JOD",
    "JP": "JPY", "KE": "KES", "KG": "KGS", "KH": "KHR", "KI": "AUD",
    "KM": "KMF", "KN": "XCD", "KP": "KPW", "KR": "KRW", "KW": "KWD",
    "KY": "KYD", "KZ": "KZT", "LA": "LAK", "LB": "LBP", "LC": "XCD",
    "LI": "CHF", "LK": "LKR", "LR": "LRD", "LT": "EUR", "LU": "EUR",
    "LV": "EUR", "LY": "LYD", "MA": "MAD", "MC": "EUR", "MD": "MDL",
    "ME": "EUR", "MF": "EUR", "MG": "MGA", "MH": "USD", "MK": "MKD",
    "ML": "XOF", "MM": "MMK", "MN": "MNT", "MO": "MOP", "MP": "USD",
    "MQ": "EUR", "MR": "MRU", "MS": "XCD", "MT": "EUR", "MU": "MUR",
    "MV": "MVR", "MW": "MWK", "MX": "MXN", "MY": "MYR", "MZ": "MZN",
    "NC": "XPF", "NE": "XOF", "NF": "AUD", "NG": "NGN", "NI": "NIO",
    "NL": "EUR", "NO": "NOK", "NP": "NPR", "NR": "AUD", "NU": "NZD",
    "NZ": "NZD", "OM": "OMR", "PE": "PEN", "PF": "XPF", "PG": "PGK",
    "PH": "PHP", "PK": "PKR", "PL": "PLN", "PM": "EUR", "PN": "NZD",
    "PR": "USD", "PT": "EUR", "PW": "USD", "PY": "PYG", "QA": "QAR",
    "RE": "EUR", "RO": "RON", "RS": "RSD", "RU": "RUB", "RW": "RWF",
    "SA": "SAR", "SB": "SBD", "SC": "SCR", "SD": "SDG", "SE": "SEK",
    "SG": "SGD", "SH": "SHP", "SI": "EUR", "SJ": "NOK", "SK": "EUR",
    "SL": "SLE", "SM": "EUR", "SN": "XOF", "SO": "SOS", "SR": "SRD",
    "SS": "SSP", "ST": "STN", "SV": "USD", "SX": "XCG", "SY": "SYP",
    "SZ": "SZL", "TC": "USD", "TD": "XAF", "TF": "EUR", "TG": "XOF",
    "TH": "THB", "TJ": "TJS", "TK": "NZD", "TL": "USD", "TM": "TMT",
    "TN": "TND", "TO": "TOP", "TR": "TRY", "TT": "TTD", "TV": "AUD",
    "TW": "TWD", "TZ": "TZS", "UA": "UAH", "UG": "UGX", "UM": "USD",
    "US": "USD", "UY": "UYU", "UZ": "UZS", "VA": "EUR", "VC": "XCD",
    "VE": "VES", "VG": "USD", "VI": "USD", "VN": "VND", "VU": "VUV",
    "WF": "XPF", "WS": "WST", "YE": "YER", "YT": "EUR", "ZA": "ZAR",
    "ZM": "ZMW",
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
        # ``type(...) is int``: ``True`` is an ``int`` too, and is not a count.
        if type(expected) is not int or expected != len(items):
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
            except ScraperError:
                # The job page is extra detail. A failed request, or an
                # answer that is not JSON (a block page), keeps the job as
                # the list gave it, without a description.
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
    soup = bs4_soup(page, scraper="ApplicantPro")
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
    """The description as plain text, one line per block of the HTML."""
    if value is None:
        return None
    cleaned = parse_html(str(value)).lines().strip()
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
    if country_iso in {"BM", "CA", "CR", "CU", "KY", "MX", "PR", "SV", "TC", "US"}:
        return "North America"
    if country_iso in _EUROPE_CODES:
        return "Europe"
    if country_iso in _ASIA_CODES:
        return "Asia"
    if country_iso in _SOUTH_AMERICA_CODES:
        return "South America"
    if country_iso in {"AU", "GU", "MP", "NZ"}:
        return "Oceania"
    if country_iso in {"BF", "CD", "EG", "MA", "ML", "MW", "NG", "SL", "TZ", "ZA"}:
        return "Africa"
    if country_iso == "AQ":
        return "Antarctica"
    return None
