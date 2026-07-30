"""Talentio public careers scraper.

Talentio employer portals expose active jobs in server-rendered React props:

    GET https://open.talentio.com/r/1/c/{namespace}/homes/{home_id}

Each active detail page includes schema.org ``JobPosting`` JSON-LD with the
full description, location, publication date, employment type, and salary:

    GET https://open.talentio.com/r/1/c/{namespace}/pages/{page_id}

No account, API key, browser, or JavaScript rendering is required.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html import escape, unescape
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import ParseResult, urlparse

from bs4 import BeautifulSoup

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

_ALLOWED_HOSTS = frozenset({"open.talentio.com", "recruit.talentio.co.jp"})
_HOME_PATH_RE = re.compile(r"^/r/1/c/([A-Za-z0-9._-]+)/homes/([1-9]\d*)$")
_PAGE_PATH_RE = re.compile(r"^/r/1/c/([A-Za-z0-9._-]+)/pages/([1-9]\d*)$")
_DETAIL_CONCURRENCY = 3
_DETAIL_HANDLED = frozenset({404, 410})
_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "CONTRACT": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "正社員": "FULL_TIME",
    "パート": "PART_TIME",
    "アルバイト": "PART_TIME",
    "契約社員": "CONTRACT",
    "業務委託": "CONTRACT",
    "派遣社員": "TEMPORARY",
    "インターン": "INTERN",
}
_SALARY_PERIOD_MAP = {
    "HOUR": "HOUR",
    "DAY": "DAY",
    "WEEK": "WEEK",
    "MONTH": "MONTH",
    "YEAR": "YEAR",
}
_REGION_BY_COUNTRY = {
    "AE": "Asia",
    "AU": "Oceania",
    "CA": "North America",
    "CN": "Asia",
    "DE": "Europe",
    "FR": "Europe",
    "GB": "Europe",
    "HK": "Asia",
    "ID": "Asia",
    "IN": "Asia",
    "JP": "Asia",
    "KR": "Asia",
    "MY": "Asia",
    "NZ": "Oceania",
    "PH": "Asia",
    "PK": "Asia",
    "SA": "Asia",
    "SG": "Asia",
    "TH": "Asia",
    "TW": "Asia",
    "US": "North America",
    "VN": "Asia",
}
_COUNTRY_NAME_TO_ISO = {
    "japan": "JP",
    "日本": "JP",
    "singapore": "SG",
    "シンガポール": "SG",
    "united arab emirates": "AE",
    "uae": "AE",
    "アラブ首長国連邦": "AE",
}
_REMOTE_RE = re.compile(r"(?:full[ -]?remote|remote|フルリモート|完全在宅)", re.I)


@ScraperRegistry.register(ATSType.TALENTIO)
class TalentioScraper(BaseScraper):
    """Scrape one public Talentio recruitment home."""

    ats = ATSType.TALENTIO

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml",
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
        self.portal_url, self.namespace, self.home_id = _parse_home_url(company_slug)
        self.company_name = company_name.strip() if company_name and company_name.strip() else None

    async def afetch(self) -> list[Job]:
        async with self.make_fetcher() as fetch:
            html = await fetch.get_text(self.portal_url)
            jobs = self._parse_listing(html)
            if self.include_descriptions and jobs:
                semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)
                keep = await asyncio.gather(
                    *(self._enrich_detail(fetch, semaphore, job) for job in jobs)
                )
                jobs = [job for job, retained in zip(jobs, keep, strict=True) if retained]
        return jobs

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description

        async def run() -> str | None:
            async with self.make_fetcher() as fetch:
                await self._enrich_detail(fetch, asyncio.Semaphore(1), job)
            return job.description

        return self._run_sync(run())

    def _parse_listing(self, html: str) -> list[Job]:
        payload = _react_payload(html, "RecruitmentOpenPageHomeView")
        company = _required_dict(payload, "openAtsCompany", "home")
        home = _required_dict(payload, "recruitmentOpenPageHome", "home")
        if company.get("openAtsNamespace") != self.namespace:
            raise ScraperError(
                f"Talentio ({self.namespace}/{self.home_id}) returned a namespace mismatch"
            )
        if _positive_int(home.get("id")) != self.home_id:
            raise ScraperError(
                f"Talentio ({self.namespace}/{self.home_id}) returned a home ID mismatch"
            )

        company_name = self.company_name or _company_name_from_html(html) or self.namespace
        language = _language(home.get("language"))
        groups = home.get("recruitmentPageGroups")
        if not isinstance(groups, list):
            raise ScraperError(
                f"Talentio ({self.namespace}/{self.home_id}) omitted recruitment groups"
            )

        jobs: list[Job] = []
        jobs_by_id: dict[str, Job] = {}
        fetched_at = datetime.now(UTC)
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise ScraperError(
                    f"Talentio ({self.namespace}/{self.home_id}) group "
                    f"{group_index} was not an object"
                )
            pages = group.get("recruitmentOpenPages")
            if not isinstance(pages, list):
                raise ScraperError(
                    f"Talentio ({self.namespace}/{self.home_id}) group "
                    f"{group_index} omitted recruitment pages"
                )
            department = _optional_text(group.get("name"))
            for page_index, page in enumerate(pages):
                if not isinstance(page, dict):
                    raise ScraperError(
                        f"Talentio ({self.namespace}/{self.home_id}) page "
                        f"{group_index}:{page_index} was not an object"
                    )
                page_id = _positive_int(page.get("id"))
                title = _optional_text(page.get("name"))
                if page_id is None or title is None:
                    raise ScraperError(
                        f"Talentio ({self.namespace}/{self.home_id}) page "
                        f"{group_index}:{page_index} omitted its ID or title"
                    )
                ats_id = str(page_id)
                job_url = _validate_page_url(
                    page.get("publishedUrl"),
                    namespace=self.namespace,
                    page_id=page_id,
                    suffix="",
                )
                apply_url = _validate_page_url(
                    page.get("publishedApplyUrl"),
                    namespace=self.namespace,
                    page_id=page_id,
                    suffix="/apply",
                )
                requisition_id = _positive_int(page.get("requisitionId"))
                form_id = _positive_int(page.get("formId"))
                existing = jobs_by_id.get(ats_id)
                if existing is not None:
                    if (
                        existing.title != title
                        or str(existing.url) != job_url
                        or str(existing.apply_url) != apply_url
                        or existing.requisition_id
                        != (str(requisition_id) if requisition_id else None)
                    ):
                        raise ScraperError(
                            f"Talentio ({self.namespace}/{self.home_id}) returned "
                            f"conflicting page data for ID {ats_id}"
                        )
                    existing.department = _merge_labels(existing.department, department)
                    continue
                job = Job(
                    url=job_url,
                    title=title,
                    company=company_name,
                    ats_type=ATSType.TALENTIO,
                    ats_id=ats_id,
                    department=department,
                        requisition_id=str(requisition_id) if requisition_id else None,
                        apply_url=apply_url,
                        language=language,
                    fetched_at=fetched_at,
                    raw={
                        "namespace": self.namespace,
                        "home_id": self.home_id,
                        "form_id": form_id,
                    },
                )
                jobs_by_id[ats_id] = job
                jobs.append(job)
        return jobs

    async def _enrich_detail(
        self,
        fetch: Fetcher,
        semaphore: asyncio.Semaphore,
        job: Job,
    ) -> bool:
        async with semaphore:
            try:
                response = await fetch.request(
                    "GET",
                    str(job.url),
                    handled=_DETAIL_HANDLED,
                )
            except ScraperError:
                return True
        if response.status_code in _DETAIL_HANDLED:
            return False
        detail = _job_posting_json_ld(response.text)
        if detail is not None:
            _apply_detail(job, detail, namespace=self.namespace)
        else:
            return _apply_react_detail(job, response.text, namespace=self.namespace)
        return True


def _parse_home_url(value: str) -> tuple[str, str, int]:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ScraperError(
            "TalentioScraper requires an HTTPS Talentio /r/1/c/{namespace}/homes/{id} URL"
        )
    match = _HOME_PATH_RE.fullmatch(parsed.path.rstrip("/"))
    if match is None:
        raise ScraperError(
            "TalentioScraper requires an HTTPS Talentio /r/1/c/{namespace}/homes/{id} URL"
        )
    namespace, home_id = match.groups()
    canonical = parsed._replace(path=parsed.path.rstrip("/")).geturl()
    return canonical, namespace, int(home_id)


def _react_payload(html: str, component: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one(f'[data-react-class*="{component}"][data-react-props]')
    if element is None:
        raise ScraperError(f"Talentio response omitted {component} bootstrap")
    try:
        payload = json.loads(unescape(str(element["data-react-props"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ScraperError(f"Talentio {component} bootstrap was malformed") from exc
    if not isinstance(payload, dict):
        raise ScraperError(f"Talentio {component} bootstrap was not an object")
    return payload


def _required_dict(payload: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ScraperError(f"Talentio {context} bootstrap omitted {key}")
    return value


def _company_name_from_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if " / " not in title:
        return None
    return _optional_text(title.rsplit(" / ", 1)[-1])


def _validate_page_url(
    value: object,
    *,
    namespace: str,
    page_id: int,
    suffix: str,
) -> str:
    if not isinstance(value, str):
        raise ScraperError(f"Talentio page {page_id} omitted its published URL")
    parsed = urlparse(value.strip())
    expected_path = f"/r/1/c/{namespace}/pages/{page_id}{suffix}"
    if not _valid_official_url(parsed) or parsed.path.rstrip("/") != expected_path.rstrip("/"):
        raise ScraperError(f"Talentio page {page_id} returned an invalid published URL")
    if suffix:
        return parsed._replace(path=expected_path).geturl()
    match = _PAGE_PATH_RE.fullmatch(parsed.path.rstrip("/"))
    if match is None or match.groups() != (namespace, str(page_id)):
        raise ScraperError(f"Talentio page {page_id} returned an identity mismatch")
    return parsed._replace(path=expected_path).geturl()


def _valid_official_url(parsed: ParseResult) -> bool:
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _ALLOWED_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
    )


def _job_posting_json_ld(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        candidates: list[Any]
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
            candidates = payload["@graph"]
        else:
            candidates = [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _apply_detail(job: Job, detail: dict[str, Any], *, namespace: str) -> None:
    identifier = detail.get("identifier")
    if (
        not isinstance(identifier, dict)
        or str(identifier.get("value") or "") != job.ats_id
    ):
        raise ScraperError(f"Talentio detail {job.ats_id} returned an ID mismatch")
    _validate_page_url(
        detail.get("url"),
        namespace=namespace,
        page_id=int(job.ats_id),
        suffix="",
    )

    title = _optional_text(detail.get("title"))
    if title is None or title != job.title:
        raise ScraperError(f"Talentio detail {job.ats_id} returned a title mismatch")
    description = _optional_text(detail.get("description"))
    if description:
        job.description = description[:25_000]

    organization = detail.get("hiringOrganization")
    if isinstance(organization, dict):
        company = _optional_text(organization.get("name"))
        if company:
            job.company = company

    location, country_iso = _location(detail.get("jobLocation"))
    if location:
        job.location = location
        if _REMOTE_RE.search(location):
            job.is_remote = True
    if country_iso:
        job.country_iso = country_iso
        job.region = _REGION_BY_COUNTRY.get(country_iso)

    posted_at = _parse_datetime(detail.get("datePosted"))
    if posted_at is not None:
        job.posted_at = posted_at

    employment = detail.get("employmentType")
    if isinstance(employment, list):
        employment = next((value for value in employment if isinstance(value, str)), None)
    commitment = _optional_text(employment)
    if commitment:
        job.commitment = commitment
        normalized = commitment.upper()
        job.employment_type = _EMPLOYMENT_TYPE_MAP.get(normalized) or next(
            (
                mapped
                for marker, mapped in _EMPLOYMENT_TYPE_MAP.items()
                if marker in commitment
            ),
            None,
        )

    _apply_salary(job, detail.get("baseSalary"))


def _apply_react_detail(job: Job, html: str, *, namespace: str) -> bool:
    payload = _react_payload(html, "RecruitmentOpenPageView")
    company = _required_dict(payload, "openAtsCompany", "detail")
    page = _required_dict(payload, "recruitmentOpenPage", "detail")
    if company.get("openAtsNamespace") != namespace:
        raise ScraperError(f"Talentio detail {job.ats_id} returned a namespace mismatch")
    page_id = _positive_int(page.get("id"))
    if page_id is None or str(page_id) != job.ats_id:
        raise ScraperError(f"Talentio detail {job.ats_id} returned an ID mismatch")
    title = _optional_text(page.get("name"))
    if title is None or title != job.title:
        raise ScraperError(f"Talentio detail {job.ats_id} returned a title mismatch")
    _validate_page_url(
        page.get("publishedUrl"),
        namespace=namespace,
        page_id=page_id,
        suffix="",
    )
    apply_url = _validate_page_url(
        page.get("publishedApplyUrl"),
        namespace=namespace,
        page_id=page_id,
        suffix="/apply",
    )
    if str(job.apply_url) != apply_url:
        raise ScraperError(f"Talentio detail {job.ats_id} returned an apply URL mismatch")
    requisition_id = _positive_int(page.get("requisitionId"))
    if requisition_id is not None and job.requisition_id != str(requisition_id):
        raise ScraperError(
            f"Talentio detail {job.ats_id} returned a requisition ID mismatch"
        )

    description = _react_description(page)
    if description is None:
        return False
    job.description = description[:25_000]
    company_name = _company_name_from_html(html)
    if company_name:
        job.company = company_name
    requisition = page.get("requisition")
    requisition_language = (
        requisition.get("language") if isinstance(requisition, dict) else None
    )
    language = _language(payload.get("language")) or _language(requisition_language)
    if language:
        job.language = language
    location = _react_location(page)
    if location:
        job.location = location
        if _REMOTE_RE.search(location):
            job.is_remote = True
    return True


def _react_description(page: dict[str, Any]) -> str | None:
    sections: list[str] = []
    for key in (
        "requisitionDetails",
        "jobDescriptionDetails",
        "requisitionCompanyAttributes",
    ):
        values = page.get(key)
        if not isinstance(values, list):
            raise ScraperError(f"Talentio detail omitted {key}")
        for field in values:
            if not isinstance(field, dict):
                raise ScraperError(f"Talentio detail {key} contained a malformed field")
            if field.get("selected") is False:
                continue
            name = _optional_text(field.get("name"))
            rendered = _render_react_value(field.get("value"))
            if rendered is None:
                continue
            heading = f"<h2>{escape(name)}</h2>" if name else ""
            sections.append(f"{heading}<p>{rendered}</p>")
    embedded = page.get("embeddedNote")
    if isinstance(embedded, dict):
        tags = embedded.get("htmlTags")
        if isinstance(tags, list):
            for tag in tags:
                rendered = _render_react_value(tag)
                if rendered:
                    sections.append(f"<p>{rendered}</p>")
    return "".join(sections) or None


def _render_react_value(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return escape(text).replace("\n", "<br>") if text else None
    if isinstance(value, list):
        rendered = [_render_react_value(item) for item in value]
        return "<br>".join(item for item in rendered if item) or None
    if value is None:
        return None
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return escape(text.strip()) or None


def _react_location(page: dict[str, Any]) -> str | None:
    locations: list[str] = []
    for key in (
        "requisitionDetails",
        "jobDescriptionDetails",
        "requisitionCompanyAttributes",
    ):
        values = page.get(key)
        if not isinstance(values, list):
            continue
        for field in values:
            if not isinstance(field, dict) or field.get("selected") is False:
                continue
            name = _optional_text(field.get("name"))
            if name is None or not re.search(
                r"(?:勤務地|勤務場所|就業場所|location|workplace)",
                name,
                re.I,
            ):
                continue
            value = field.get("value")
            if isinstance(value, list):
                locations.extend(
                    text
                    for item in value
                    if (text := _optional_text(item))
                )
            elif text := _optional_text(value):
                locations.append(text)
    return "; ".join(dict.fromkeys(locations)) or None


def _apply_salary(job: Job, value: object) -> None:
    if not isinstance(value, dict):
        return
    currency = _optional_text(value.get("currency"))
    amount = value.get("value")
    if not isinstance(amount, dict) or currency is None or len(currency) != 3:
        return
    minimum = _number(amount.get("minValue"))
    maximum = _number(amount.get("maxValue"))
    if minimum is None and maximum is None:
        scalar = _number(amount.get("value"))
        minimum = scalar
        maximum = scalar
    if minimum is None and maximum is None:
        return
    period = _SALARY_PERIOD_MAP.get(str(amount.get("unitText") or "").upper(), "YEAR")
    job.salary_currency = currency.upper()
    job.salary_period = period
    job.salary_min = minimum
    job.salary_max = maximum


def _location(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, list):
        locations = [_location(item) for item in value]
        texts = [text for text, _ in locations if text]
        countries = {country for _, country in locations if country}
        country_iso = next(iter(countries)) if len(countries) == 1 else None
        return "; ".join(dict.fromkeys(texts)) or None, country_iso
    if not isinstance(value, dict):
        return None, None
    address = value.get("address")
    if isinstance(address, str):
        return _optional_text(address), None
    if not isinstance(address, dict):
        return None, None
    parts = [
        str(address[key]).strip()
        for key in (
            "streetAddress",
            "addressLocality",
            "addressRegion",
            "postalCode",
            "addressCountry",
        )
        if address.get(key)
    ]
    return ", ".join(parts) or None, _country_iso(address.get("addressCountry"))


def _country_iso(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    text = _optional_text(value)
    if text is None:
        return None
    upper = text.upper()
    if re.fullmatch(r"[A-Z]{2}", upper):
        return upper
    return _COUNTRY_NAME_TO_ISO.get(text.casefold())


def _parse_datetime(value: object) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and str(value).strip() == str(parsed) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _language(value: object) -> str | None:
    text = _optional_text(value)
    return text.lower() if text and re.fullmatch(r"[A-Za-z]{2}", text) else None


def _merge_labels(first: str | None, second: str | None) -> str | None:
    labels = [
        label.strip()
        for value in (first, second)
        if value
        for label in value.split(";")
        if label.strip()
    ]
    return "; ".join(dict.fromkeys(labels)) or None
