"""SAP SuccessFactors careers scraper.

Used by Procter & Gamble, Pfizer, Daimler, Schindler, and many others.

SuccessFactors exposes two credential-free public job feeds:

* Recruiting Marketing sites expose RSS 2.0 at the canonical
  (typo-included, undocumented but stable) path:

      GET https://{recruiting-marketing-host}/sitemal.xml

* Legacy Recruiting Management sites expose ``Job-Listing`` XML:

      GET https://career{N}.successfactors.com/career
          ?company={ID}
          &career_ns=job_listing_summary
          &resultType=XML

Pass either a recruiting-marketing host or a public legacy career URL as
``company_slug``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.etree import ElementTree as ET

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    pass

_TAG_RE = re.compile(r"<[^>]+>")
_TEMPLATE_TOKEN_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_INVALID_EMPTY_ELEMENT_RE = re.compile(r"<>\s*[^<]*?</>")
_EMPTY_LEGACY_VALUES = {"", "n/a", "na", "none", "not applicable"}
# Job titles are often `"Title (City, State, Country)"` — extract location
# from the trailing parens when no other source is available.
_TITLE_LOCATION_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<loc>[^()]+)\)\s*$")
_LEGACY_LOCATION_COUNTRIES = {
    "australia",
    "brazil",
    "canada",
    "china",
    "denmark",
    "france",
    "germany",
    "india",
    "ireland",
    "italy",
    "japan",
    "malaysia",
    "mexico",
    "netherlands",
    "poland",
    "romania",
    "singapore",
    "south africa",
    "spain",
    "sweden",
    "switzerland",
    "taiwan",
    "thailand",
    "united arab emirates",
    "united kingdom",
    "united states",
    "vietnam",
}
# Google Merchant namespace
_GOOGLE_NS = {"g": "http://base.google.com/ns/1.0"}

_EMPLOYMENT_TYPE_PATTERNS = {
    "intern": "INTERN",
    "internship": "INTERN",
    "apprentice": "INTERN",
    "trainee": "INTERN",
    "contract": "CONTRACT",
    "fixed-term": "CONTRACT",
    "fixed term": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
}


@ScraperRegistry.register(ATSType.SUCCESSFACTORS)
class SuccessFactorsScraper(BaseScraper):
    """SAP SuccessFactors Recruiting Marketing and legacy XML scraper.

    ``company_slug`` may be a recruiting-marketing host or a legacy public
    career URL containing the tenant's ``company`` query parameter.
    """

    ats = ATSType.SUCCESSFACTORS

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml, application/xml, text/xml",
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

    async def afetch(self) -> list[Job]:
        feed_url, legacy_company_id = self._resolve_feed_target()
        async with self.make_fetcher() as fetch:
            xml_text = await fetch.get_text(feed_url)
        if legacy_company_id is not None:
            return self._parse_legacy_feed(
                xml_text,
                feed_url=feed_url,
                company_id=legacy_company_id,
            )
        return self._parse_feed(xml_text)

    def _resolve_feed_target(self) -> tuple[str, str | None]:
        parsed = urlparse(self.company_slug)
        query = parse_qs(parsed.query)
        company_values = query.get("company")
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and _is_legacy_host(parsed.hostname)
            and company_values
            and company_values[0].strip()
        ):
            company_id = company_values[0].strip()
            legacy_query = {
                "company": company_id,
                "career_ns": "job_listing_summary",
                "resultType": "XML",
            }
            locale_values = query.get("rcm_site_locale")
            if locale_values and locale_values[0].strip():
                legacy_query["rcm_site_locale"] = locale_values[0].strip()
            feed_url = urlunparse(
                (
                    "https",
                    parsed.hostname,
                    "/career",
                    "",
                    urlencode(legacy_query),
                    "",
                )
            )
            return feed_url, company_id
        return self._resolve_feed_url(), None

    def _resolve_feed_url(self) -> str:
        slug = self.company_slug
        if slug.startswith(("http://", "https://")):
            parsed = urlparse(slug)
            base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            base = base.rstrip("/")
        elif "." in slug:
            base = f"https://{slug}"
        else:
            base = f"https://job.{slug}.com"
        return f"{base}/sitemal.xml"

    def _parse_legacy_feed(
        self,
        xml_text: str,
        *,
        feed_url: str,
        company_id: str,
    ) -> list[Job]:
        try:
            root = _parse_xml(xml_text)
        except ET.ParseError as exc:
            raise ScraperError(
                f"SuccessFactors returned malformed XML: {exc}"
            ) from exc
        if root.tag != "Job-Listing":
            raise ScraperError(
                f"SuccessFactors returned non-job-listing XML for "
                f"{self.company_slug} (root <{root.tag}>)"
            )

        parsed_feed = urlparse(feed_url)
        feed_host = parsed_feed.hostname or ""
        company = self.company_name or company_id
        day_first_dates = _legacy_dates_are_day_first(root)
        jobs: list[Job] = []
        seen: set[str] = set()
        for item in root.findall("./Job"):
            requisition_id = _first_text(item.findtext("ReqId"))
            title_raw = _first_text(item.findtext("JobTitle"))
            if not requisition_id or not title_raw or requisition_id in seen:
                continue
            title, title_location = _split_legacy_title_location(
                html.unescape(title_raw)
            )
            seen.add(requisition_id)
            job_url = urlunparse(
                (
                    "https",
                    feed_host,
                    "/sfcareer/jobreqcareer",
                    "",
                    urlencode(
                        {"jobId": requisition_id, "company": company_id}
                    ),
                    "",
                )
            )
            description = None
            if self.include_descriptions:
                replacements = {
                    child.tag: value
                    for child in item
                    if (value := _legacy_field_value(child))
                }
                replacements["id"] = requisition_id
                description = _clean_description(
                    _render_template(
                        item.findtext("Job-Description"),
                        replacements,
                    )
                )
            jobs.append(
                Job(
                    url=job_url,
                    title=title,
                    company=company,
                    ats_type=ATSType.SUCCESSFACTORS,
                    ats_id=f"{company_id}:{requisition_id}",
                    requisition_id=requisition_id,
                    location=_legacy_location(item) or title_location,
                    description=description,
                    department=_legacy_department(item),
                    posted_at=_parse_legacy_date(
                        item.findtext("Posted-Date"),
                        day_first=day_first_dates,
                    ),
                    fetched_at=datetime.now(UTC),
                )
            )
        return jobs

    def _parse_feed(self, xml_text: str) -> list[Job]:
        try:
            root = _parse_xml(xml_text)
        except ET.ParseError as exc:
            raise ScraperError(
                f"SuccessFactors returned malformed XML: {exc}"
            ) from exc

        # Some tenants front the feed with an HTML error page that parses
        # as XML but isn't RSS. Catch that.
        if root.tag.lower() != "rss" and root.find(".//channel") is None:
            raise ScraperError(
                f"SuccessFactors returned non-RSS XML for {self.company_slug} "
                f"(root <{root.tag}>); tenant may not expose sitemal.xml"
            )

        company = self._derive_company_name(root)
        host = urlparse(self._resolve_feed_url()).hostname or ""

        jobs: list[Job] = []
        seen: set[str] = set()
        for item in root.iter("item"):
            job = self._parse_item(item, company=company, host=host)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _derive_company_name(self, root: ET.Element) -> str:
        title = root.findtext(".//channel/title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        return self.company_slug

    def _parse_item(
        self, item: ET.Element, *, company: str, host: str
    ) -> Job | None:
        link = (item.findtext("link") or "").strip()
        if not link:
            return None
        # ats_id: prefer the Google ID, else the trailing numeric/hash from URL.
        gid = item.findtext("g:id", namespaces=_GOOGLE_NS)
        ats_id = (gid or "").strip()
        if not ats_id:
            tail = link.rstrip("/").rsplit("/", 1)[-1]
            ats_id = re.split(r"[?&#]", tail, maxsplit=1)[0] or link
        guid = item.findtext("guid")
        if not ats_id and guid:
            ats_id = guid.strip()

        title_raw = (item.findtext("title") or "").strip() or "Untitled"
        title, location = _split_title_location(title_raw)

        # Prefer Google namespace location when present.
        if not location:
            location = _first_text(
                item.findtext("g:location", namespaces=_GOOGLE_NS),
            )

        description = _clean_description(item.findtext("description"))
        posted_at = _parse_pubdate(item.findtext("pubDate"))

        # Most SuccessFactors RSS feeds omit ``pubDate`` and instead
        # carry only ``g:expiration_date``. We don't surface that as
        # ``posted_at`` (it would be misleading) but a small subset of
        # tenants ship a non-namespaced ``date`` element with the post
        # date.
        if posted_at is None:
            for tag in ("postDate", "publishedDate", "pubdate", "date"):
                v = item.findtext(tag)
                if v:
                    posted_at = _parse_pubdate(v)
                    if posted_at:
                        break

        # ``g:employer`` is the actual employer name (the recruiting-
        # marketing channel title is typically the parent brand).
        employer = _first_text(item.findtext("g:employer", namespaces=_GOOGLE_NS))
        if employer:
            company = employer

        # ``g:job_function`` is the closest analog to a ``department``
        # facet (categories like ``Professionals`` / ``Engineering`` /
        # ``Sales``).
        department = _first_text(
            item.findtext("g:job_function", namespaces=_GOOGLE_NS),
        )

        # ``g:job_type`` (rare) — when present, map to the canonical
        # employment-type enum.
        employment_type: str | None = None
        commitment: str | None = None
        job_type_text = _first_text(
            item.findtext("g:job_type", namespaces=_GOOGLE_NS),
        ) or _first_text(item.findtext("g:employment_type", namespaces=_GOOGLE_NS))
        if job_type_text:
            commitment = job_type_text
            norm = job_type_text.lower()
            for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
                if needle in norm:
                    employment_type = mapped
                    break

        return Job(
            url=link,
            title=title,
            company=company,
            ats_type=ATSType.SUCCESSFACTORS,
            ats_id=ats_id,
            location=location,
            description=description,
            department=department,
            employment_type=employment_type,
            commitment=commitment,
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
        )


def _split_title_location(raw: str) -> tuple[str, str | None]:
    """Some tenants format titles as ``"Title (City, State, Country)"``.
    Strip the parens into a separate location. Leave the title untouched
    when the trailing parens look like a department/category instead."""
    match = _TITLE_LOCATION_RE.match(raw)
    if not match:
        return raw, None
    inner = match.group("loc").strip()
    # Heuristic: a location usually has a comma OR ends in a 2-letter
    # country/state code. Reject single-word parens like "(Remote)".
    if "," in inner or re.search(r"\b[A-Z]{2}\b", inner):
        return match.group("title").strip(), inner
    return raw, None


def _split_legacy_title_location(raw: str) -> tuple[str, str | None]:
    match = _TITLE_LOCATION_RE.match(raw)
    if not match:
        return raw, None
    inner = match.group("loc").strip()
    parts = [part.strip() for part in inner.split(",")]
    if len(parts) < 2:
        return raw, None
    tail = parts[-1]
    is_country = tail.casefold() in _LEGACY_LOCATION_COUNTRIES
    is_three_letter_code = re.fullmatch(r"[A-Z]{3}", tail) is not None
    has_state_and_country_codes = (
        len(parts) >= 3
        and re.fullmatch(r"[A-Z]{2}", parts[-2]) is not None
        and re.fullmatch(r"[A-Z]{2}", tail) is not None
    )
    if not (is_country or is_three_letter_code or has_state_and_country_codes):
        return raw, None
    return match.group("title").strip(), inner


def _first_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def _legacy_field_value(element: ET.Element) -> str | None:
    return _first_text(element.text) or _first_text(element.findtext("value"))


def _legacy_labeled_fields(item: ET.Element) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for child in item:
        label = _first_text(child.findtext("label"))
        value = _first_text(child.findtext("value"))
        if (
            not label
            or not value
            or _legacy_value_is_empty(value)
        ):
            continue
        fields.append((label, value))
    return fields


def _legacy_value_is_empty(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return (
        normalized in _EMPTY_LEGACY_VALUES
        or "not applicable" in normalized
    )


def _legacy_location(item: ET.Element) -> str | None:
    direct = _first_text(item.findtext("Location"))
    if direct and not _legacy_value_is_empty(direct):
        return html.unescape(direct)

    generic_locations: list[str] = []
    specific_locations: list[str] = []
    cities: list[str] = []
    states: list[str] = []
    countries: list[str] = []
    for label, value in _legacy_labeled_fields(item):
        normalized = re.sub(r"[^a-z]+", " ", label.casefold()).strip()
        if "additional" in normalized:
            continue
        if normalized == "location":
            generic_locations.append(value)
        elif "location" in normalized:
            specific_locations.append(value)
        elif "city" in normalized or "town" in normalized:
            cities.append(value)
        elif "state" in normalized or "province" in normalized:
            states.append(value)
        elif "country" in normalized or normalized == "region":
            countries.append(value)

    if specific_locations:
        return html.unescape(specific_locations[0])

    if generic_locations:
        generic = generic_locations[0]
        generic_normalized = generic.casefold()
        atomic_values = [*cities, *states, *countries]
        if "," in generic or (
            len(atomic_values) >= 2
            and all(
                value.casefold() in generic_normalized
                for value in atomic_values
            )
        ):
            return html.unescape(generic)

    components: list[str] = []
    seen_components: set[str] = set()
    for value in (*cities, *states, *countries, *generic_locations):
        normalized = value.casefold()
        if normalized not in seen_components:
            components.append(value)
            seen_components.add(normalized)
    if not components:
        return None
    return html.unescape(", ".join(components))


def _legacy_department(item: ET.Element) -> str | None:
    for tag in ("Department", "Division"):
        direct = _first_text(item.findtext(tag))
        if direct and not _legacy_value_is_empty(direct):
            return html.unescape(direct)
    for label, value in _legacy_labeled_fields(item):
        normalized = re.sub(r"[^a-z]+", " ", label.casefold()).strip()
        if normalized in {"department", "job function", "function"}:
            return html.unescape(value)
    return None


def _is_legacy_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized.endswith(
        (
            ".successfactors.com",
            ".successfactors.eu",
            ".sapsf.com",
            ".sapsf.eu",
            ".sapsf.cn",
        )
    )


def _parse_xml(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError:
        sanitized = _INVALID_EMPTY_ELEMENT_RE.sub("", xml_text)
        if sanitized == xml_text:
            raise
        return ET.fromstring(sanitized)


def _render_template(
    value: object,
    replacements: dict[str, str],
) -> object:
    if not isinstance(value, str):
        return value
    return _TEMPLATE_TOKEN_RE.sub(
        lambda match: replacements.get(match.group(1), ""),
        value,
    )


def _clean_description(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:25_000] or None


def _parse_pubdate(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _legacy_dates_are_day_first(root: ET.Element) -> bool:
    day_first_votes = 0
    month_first_votes = 0
    for value in root.iterfind("./Job/Posted-Date"):
        parts = (value.text or "").strip().split("/")
        if len(parts) != 3:
            continue
        try:
            first, second = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if first > 12 >= second:
            day_first_votes += 1
        elif second > 12 >= first:
            month_first_votes += 1
    return day_first_votes > month_first_votes


def _parse_legacy_date(
    value: object,
    *,
    day_first: bool = False,
) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    slash_format = "%d/%m/%Y" if day_first else "%m/%d/%Y"
    for date_format in (slash_format, "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), date_format).replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
    return None
