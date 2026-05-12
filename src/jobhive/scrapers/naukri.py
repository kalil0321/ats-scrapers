"""Naukri.com India direct job board API scraper."""

from __future__ import annotations

import base64
import os
import re
import time
import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

try:
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA
    from curl_cffi import requests as cffi_requests
    from curl_cffi.requests.exceptions import CurlError
except ModuleNotFoundError:  # pragma: no cover - exercised only without scrapers extra.
    PKCS1_v1_5 = None  # type: ignore[assignment]
    RSA = None  # type: ignore[assignment]
    cffi_requests = None  # type: ignore[assignment]
    CurlError = Exception  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any


SEARCH_URL = "https://www.naukri.com/jobapi/v3/search"
BASE_URL = "https://www.naukri.com"
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 10
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 0.8

DEFAULT_KEYWORDS = (
    "software engineer",
    "java developer",
    "python developer",
    "data scientist",
    "data analyst",
    "devops engineer",
    "product manager",
    "sales",
    "accountant",
    "customer support",
    "operations",
    "marketing",
)

DEFAULT_LOCATIONS = (
    "",
    "bangalore",
    "hyderabad",
    "pune",
    "mumbai",
    "delhi ncr",
    "chennai",
    "gurgaon",
    "noida",
    "kolkata",
    "ahmedabad",
)

_NAUKRI_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALrlQ+djR0RjJwBF1xuisHmdFv334MIm
K6LgzJhmLhN7B5yuEyaKoasgXQk3+OQglsOaBxEJ0j5PcTL3nbOvt80CAwEAAQ==
-----END PUBLIC KEY-----"""


@ScraperRegistry.register(ATSType.NAUKRI)
class NaukriScraper(BaseScraper):
    """Naukri India scraper using its internal JSON search API.

    `reverse-api-engineer` found `GET /jobapi/v3/search` on 2026-05-11.
    The endpoint is public but Akamai rejects many datacenter IPs, so the
    scraper supports `proxy_url` or `JOBHIVE_NAUKRI_PROXY` / `PROXY`.
    Single-source scraper: `company_slug` is informational and ignored.
    """

    ats = ATSType.NAUKRI

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        keywords: Iterable[str] = DEFAULT_KEYWORDS,
        locations: Iterable[str] = DEFAULT_LOCATIONS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        proxy_url: str | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.keywords = tuple(keywords)
        self.locations = tuple(locations)
        self.page_size = min(page_size, DEFAULT_PAGE_SIZE)
        self.max_pages = max_pages
        self.proxy_url = proxy_url or _env_proxy_url()

    def fetch(self) -> list[Job]:
        if cffi_requests is None or PKCS1_v1_5 is None or RSA is None:
            raise ScraperError(
                "Naukri requires the scrapers extra: install curl_cffi and pycryptodome"
            )

        session = cffi_requests.Session(impersonate="chrome136")
        if self.proxy_url:
            session.proxies = {"http": self.proxy_url, "https": self.proxy_url}

        jobs: list[Job] = []
        seen: set[str] = set()
        for keyword in self.keywords:
            for location in self.locations:
                for page in range(1, self.max_pages + 1):
                    payload = self._fetch_page(
                        session,
                        keyword=keyword,
                        location=location,
                        page=page,
                    )
                    items = payload.get("jobDetails") or []
                    if not items:
                        break
                    for item in items:
                        job = self._parse_item(item)
                        if job is None or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)
                    total = int(payload.get("noOfJobs") or 0)
                    fetched = (page - 1) * self.page_size + len(items)
                    if fetched >= total or len(items) < self.page_size:
                        break
        return jobs

    def _fetch_page(
        self,
        session: Any,
        *,
        keyword: str,
        location: str,
        page: int,
    ) -> dict[str, Any]:
        params = _search_params(
            keyword=keyword,
            location=location,
            page=page,
            page_size=self.page_size,
        )
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            headers = _headers(keyword, location)
            try:
                response = session.get(
                    SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except CurlError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        "Naukri API request failed. If this is a datacenter IP, "
                        "set JOBHIVE_NAUKRI_PROXY to a residential proxy."
                    ) from exc
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError("Naukri returned invalid JSON") from exc
            if response.status_code == 403 and attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            raise ScraperError(
                f"Naukri returned HTTP {response.status_code}: {response.text[:200]}"
            )
        raise ScraperError(f"Naukri exhausted retries: {last_exc}")

    def _parse_item(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("jobId") or "").strip()
        title = _clean(item.get("title"))
        if not ats_id or not title:
            return None

        placeholders = {
            str(p.get("type") or ""): _clean(p.get("label"))
            for p in item.get("placeholders", [])
            if isinstance(p, dict)
        }
        location = placeholders.get("location") or _clean(item.get("locations"))
        salary_summary = placeholders.get("salary") or _clean(item.get("salary"))
        salary_min, salary_max = _parse_salary(salary_summary)
        url = _absolute_url(_clean(item.get("jdURL")), ats_id)
        created_date = _parse_epoch_ms(item.get("createdDate"))
        company = _clean(item.get("companyName")) or "Naukri employer"

        raw = {
            key: value
            for key, value in {
                "company_id": item.get("companyId"),
                "company_static_url": item.get("staticUrl"),
                "apply_url": item.get("companyApplyUrl"),
                "posted_label": item.get("footerPlaceholderLabel"),
                "skills": item.get("tagsAndSkills"),
                "currency": item.get("currency"),
                "experience_label": placeholders.get("experience")
                or _clean(item.get("experienceText")),
            }.items()
            if value not in (None, "", [])
        }

        return Job(
            ats_type=ATSType.NAUKRI,
            ats_id=ats_id,
            title=title,
            company=company,
            url=url,
            apply_url=_absolute_url(_clean(item.get("companyApplyUrl")), ats_id)
            if item.get("companyApplyUrl")
            else None,
            location=location,
            country_iso="IN",
            salary_currency="INR" if salary_summary and salary_summary != "Not disclosed" else None,
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            experience=_parse_experience(
                placeholders.get("experience") or _clean(item.get("experienceText"))
            ),
            description=_clean_html(item.get("jobDescription")),
            posted_at=created_date,
            language="en",
            raw=raw or None,
        )


def _headers(keyword: str, location: str) -> dict[str, str]:
    seo_key = _seo_key(keyword, location)
    return {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "appid": "109",
        "clientid": "d3skt0p",
        "content-type": "application/json",
        "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
        "nkparam": _generate_nkparam(),
        "referer": f"{BASE_URL}/{seo_key}",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "systemid": "Naukri",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
    }


def _search_params(
    *,
    keyword: str,
    location: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "noOfResults": page_size,
        "urlType": "search_by_key_loc" if location else "search_by_keyword",
        "searchType": "adv",
        "keyword": keyword,
        "pageNo": page,
        "seoKey": _seo_key(keyword, location),
        "src": "jobsearchDesk",
        "latLong": "",
    }
    if location:
        params["location"] = location
    return params


def _seo_key(keyword: str, location: str) -> str:
    keyword_slug = urllib.parse.quote(keyword.lower().replace(" ", "-"))
    if location:
        loc_slug = urllib.parse.quote(location.lower().replace(" ", "-"))
        return f"{keyword_slug}-jobs-in-{loc_slug}"
    return f"{keyword_slug}-jobs-in-india"


def _generate_nkparam() -> str:
    if PKCS1_v1_5 is None or RSA is None:
        raise ScraperError("Naukri requires pycryptodome")
    plaintext = f"v0|{int(time.time() * 1000)}|121_srp"
    cipher = PKCS1_v1_5.new(RSA.import_key(_NAUKRI_RSA_PUBLIC_KEY))
    encrypted = cipher.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _env_proxy_url() -> str | None:
    raw = os.getenv("JOBHIVE_NAUKRI_PROXY") or os.getenv("PROXY")
    return _normalise_proxy_url(raw) if raw else None


def _normalise_proxy_url(raw: str) -> str:
    if "@" in raw or not raw.startswith("http://"):
        return raw
    rest = raw.removeprefix("http://")
    parts = rest.split(":")
    if len(parts) < 4:
        return raw
    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    return (
        f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(password)}@"
        f"{host}:{port}"
    )


def _absolute_url(value: str | None, ats_id: str) -> str:
    if value:
        if value.startswith("http"):
            return value
        if value.startswith("/"):
            return f"{BASE_URL}{value}"
        return f"{BASE_URL}/{value}"
    return f"{BASE_URL}/job-listings-{ats_id}"


def _parse_epoch_ms(value: object) -> datetime | None:
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _parse_salary(value: str | None) -> tuple[float | None, float | None]:
    if not value or value.lower() in {"not disclosed", "undisclosed"}:
        return None, None
    lowered = value.lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*lacs?", lowered)
    if match:
        return float(match.group(1)) * 100000, float(match.group(2)) * 100000
    nums = re.findall(r"\d+(?:\.\d+)?", lowered)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    if len(nums) == 1:
        amount = float(nums[0])
        return amount, amount
    return None, None


def _parse_experience(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_html(value: object) -> str | None:
    text = _clean(value)
    if not text:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip() or None
