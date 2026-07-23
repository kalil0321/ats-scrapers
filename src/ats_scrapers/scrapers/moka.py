"""Moka (mokahr.com) multi-tenant ATS scraper.

Public boards use either of these forms::

    https://app.mokahr.com/social-recruitment/{slug}/{site_id}
    https://hire-r1.mokahr.com/social-recruitment/{slug}/{site_id}

The job-list API returns plaintext JSON for some tenants and an AES-CBC
envelope for others. The encrypted form carries its UTF-8 key in the
``necromancer`` field and uses the IV embedded in Moka's public bundle.
"""

from __future__ import annotations

import base64
import html as html_mod
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import HttpUrl

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from ats_scrapers.fetch import Fetcher

DEFAULT_HOST = "app.mokahr.com"
HIRE_R1_HOST = "hire-r1.mokahr.com"
_HOST_ALIASES = {
    "app": DEFAULT_HOST,
    DEFAULT_HOST: DEFAULT_HOST,
    "hire-r1": HIRE_R1_HOST,
    HIRE_R1_HOST: HIRE_R1_HOST,
}

API_TEMPLATE = "https://{host}/api/outer/ats-apply/website/jobs/v2"
_DEFAULT_AES_IV = b"de7c21ed8d6f50fe"
PAGE_SIZE = 50
MAX_PAGES = 200

_EMPLOYMENT_MAP: dict[str, EmploymentType] = {
    "全职": "FULL_TIME",
    "兼职": "PART_TIME",
    "实习": "INTERN",
    "实习生": "INTERN",
    "外包": "CONTRACT",
    "合同工": "CONTRACT",
    "临时": "TEMPORARY",
    "Full-Time": "FULL_TIME",
    "Part-Time": "PART_TIME",
    "Internship": "INTERN",
    "Contract": "CONTRACT",
    "Temporary": "TEMPORARY",
}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_ASCII_RE = re.compile(r"^[\x00-\x7f\s\W]+$")


@ScraperRegistry.register(ATSType.MOKA)
class MokaScraper(BaseScraper):
    """Scrape one Moka tenant encoded as ``slug/site_id``.

    A leading ``hire-r1/`` pins newer tenants to Moka's second host. An
    optional trailing ``/campus`` selects campus recruitment instead of the
    default social-recruitment board.
    """

    ats = ATSType.MOKA
    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        slug_host, slug, site_id, recruitment_type = self._parse_slug(company_slug)
        self.slug = slug
        self.site_id = site_id
        self.recruitment_type = recruitment_type
        self.host = _normalize_host(host or slug_host or DEFAULT_HOST)

    async def afetch(self) -> list[Job]:
        headers = {**self.default_headers, "Referer": self._tenant_url()}
        async with self.make_fetcher(headers=headers) as fetch:
            jobs: list[Job] = []
            seen: set[str] = set()
            offset = 0
            for _page in range(MAX_PAGES):
                payload = await self._fetch_page(
                    fetch,
                    offset=offset,
                    limit=PAGE_SIZE,
                )
                page_jobs = payload.get("jobs") or []
                if not isinstance(page_jobs, list) or not page_jobs:
                    break
                for item in page_jobs:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_job(item)
                    if job is None or not job.ats_id or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

                offset += len(page_jobs)
                total = (payload.get("jobStats") or {}).get("total")
                if isinstance(total, int) and offset >= total:
                    break
                if len(page_jobs) < PAGE_SIZE:
                    break
            return jobs

    async def _fetch_page(
        self,
        fetch: Fetcher,
        *,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        envelope = await fetch.post_json(
            API_TEMPLATE.format(host=self.host),
            json={
                "orgId": self.slug,
                "siteId": self.site_id,
                "limit": limit,
                "offset": offset,
                "needStat": True,
                "site": self.recruitment_type,
            },
        )
        return _unwrap(envelope, slug=self.company_slug)

    def _parse_job(self, item: dict[str, Any]) -> Job | None:
        raw_id = item.get("id")
        ats_id = str(raw_id).strip() if raw_id is not None else ""
        title = str(item.get("title") or "").strip()
        if not ats_id or not title:
            return None

        location, country_iso = _location(item.get("locations") or [])
        commitment = item.get("commitment")
        return Job(
            url=HttpUrl(self._job_url(ats_id)),
            title=title,
            company=self.slug,
            ats_type=ATSType.MOKA,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Asia" if country_iso in {"CN", "HK", "TW", "SG"} else None,
            language=_language(item.get("multiLocale"), title),
            employment_type=_employment_type(commitment),
            department=_department_name(item.get("department")),
            commitment=commitment if isinstance(commitment, str) else None,
            description=_strip_html(item.get("jobDescription")),
            posted_at=_parse_dt(
                item.get("publishedAt") or item.get("openedAt") or item.get("createdAt")
            ),
            fetched_at=datetime.now(UTC),
            raw=_raw_overflow(
                item,
                slug=self.slug,
                site_id=self.site_id,
                host=self.host,
            ),
        )

    def _job_url(self, job_id: str) -> str:
        return f"{self._tenant_url()}/job/{job_id}"

    def _tenant_url(self) -> str:
        board = (
            "campus-recruitment"
            if self.recruitment_type == "campus"
            else "social-recruitment"
        )
        return f"https://{self.host}/{board}/{self.slug}/{self.site_id}"

    @staticmethod
    def _parse_slug(company_slug: str) -> tuple[str | None, str, int, str]:
        parts = [part for part in company_slug.strip().strip("/").split("/") if part]
        host: str | None = None
        if parts and parts[0] in _HOST_ALIASES:
            host = _HOST_ALIASES[parts.pop(0)]
        if len(parts) < 2:
            raise ScraperError(
                f"Moka company_slug must be '<slug>/<siteId>', got {company_slug!r}"
            )
        slug = parts[0]
        try:
            site_id = int(parts[1])
        except ValueError as exc:
            raise ScraperError(f"Moka siteId must be numeric, got {parts[1]!r}") from exc
        recruitment_type = "social"
        if len(parts) >= 3:
            recruitment_type = parts[2].lower()
            if recruitment_type not in {"social", "campus"}:
                raise ScraperError(
                    "Moka recruitment type must be 'social' or 'campus', "
                    f"got {parts[2]!r}"
                )
        if len(parts) > 3:
            raise ScraperError(f"Moka company_slug has unexpected segments: {company_slug!r}")
        return host, slug, site_id, recruitment_type


def _normalize_host(host: str) -> str:
    normalized = _HOST_ALIASES.get(host)
    if normalized is None:
        raise ScraperError(f"Unsupported Moka host: {host!r}")
    return normalized


def decrypt_moka(
    data_b64: str,
    necromancer: str,
    *,
    aes_iv: bytes = _DEFAULT_AES_IV,
) -> dict[str, Any]:
    """Decrypt Moka's AES-CBC + PKCS7 response envelope."""

    try:
        from Crypto.Cipher import AES
    except ImportError as exc:  # pragma: no cover - installation failure
        raise ScraperError(
            "Moka scraper requires pycryptodome (install ats-scrapers[scrapers])"
        ) from exc

    key = necromancer.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise ScraperError(
            f"Moka necromancer key has invalid byte length {len(key)} (expected 16/24/32)"
        )
    try:
        ciphertext = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ScraperError(f"Moka data is not valid base64: {exc}") from exc
    if not ciphertext or len(ciphertext) % 16:
        raise ScraperError(
            f"Moka ciphertext length {len(ciphertext)} is not a multiple of 16"
        )

    plaintext = AES.new(key, AES.MODE_CBC, aes_iv).decrypt(ciphertext)
    pad_length = plaintext[-1]
    if pad_length < 1 or pad_length > 16:
        raise ScraperError("Moka ciphertext failed PKCS7 unpad (bad pad byte)")
    if plaintext[-pad_length:] != bytes([pad_length]) * pad_length:
        raise ScraperError("Moka ciphertext failed PKCS7 unpad (mismatch)")
    try:
        decoded = json.loads(plaintext[:-pad_length])
    except ValueError as exc:
        raise ScraperError(f"Moka decrypted body is not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ScraperError("Moka decrypted body is not an object")
    return decoded


def _unwrap(envelope: Any, *, slug: str) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ScraperError(f"Moka returned non-object envelope: {type(envelope)}")
    if envelope.get("necromancer") and isinstance(envelope.get("data"), str):
        return _unwrap(
            decrypt_moka(envelope["data"], envelope["necromancer"]),
            slug=slug,
        )
    if envelope.get("success") is False or envelope.get("code") not in {None, 0}:
        code = envelope.get("code")
        message = envelope.get("msg") or "unknown"
        if code == 703002:
            raise CompanyNotFoundError(f"Moka rejected tenant {slug}: {message}")
        raise ScraperError(f"Moka API error code={code} msg={message}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ScraperError("Moka envelope missing 'data' object")
    return data


def _location(locations: Any) -> tuple[str | None, str | None]:
    if not isinstance(locations, list):
        return None, None
    pieces: list[str] = []
    seen: set[str] = set()
    country_iso: str | None = None
    country_map = {
        "中国": "CN",
        "China": "CN",
        "香港": "HK",
        "Hong Kong": "HK",
        "台湾": "TW",
        "Taiwan": "TW",
        "新加坡": "SG",
        "Singapore": "SG",
    }
    for location in locations:
        if not isinstance(location, dict):
            continue
        display = ", ".join(
            value.strip()
            for value in (
                location.get("cityName"),
                location.get("provinceName"),
                location.get("country"),
            )
            if isinstance(value, str) and value.strip()
        )
        if display and display not in seen:
            seen.add(display)
            pieces.append(display)
        country = location.get("country")
        if isinstance(country, str):
            country_iso = country_iso or country_map.get(country)
    return "; ".join(pieces) or None, country_iso


def _strip_html(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _HTML_TAG_RE.sub(" ", value)
    cleaned = html_mod.unescape(cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()[:25_000] or None


def _employment_type(commitment: Any) -> EmploymentType | None:
    if not isinstance(commitment, str):
        return None
    return _EMPLOYMENT_MAP.get(commitment.strip())


def _department_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _language(multi_locale: Any, title: str) -> str | None:
    if isinstance(multi_locale, str) and multi_locale.strip():
        try:
            decoded = json.loads(multi_locale)
        except ValueError:
            decoded = {}
        locale = decoded.get("mainLocale") if isinstance(decoded, dict) else None
        if isinstance(locale, str) and locale:
            return locale.split("-", 1)[0].lower()
    return "en" if title and _ASCII_RE.match(title) else "zh"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _raw_overflow(
    item: dict[str, Any],
    *,
    slug: str,
    site_id: int,
    host: str,
) -> dict[str, Any]:
    overflow: dict[str, Any] = {"slug": slug, "site_id": site_id, "host": host}
    for key in (
        "attributeId",
        "deptId",
        "hireMode",
        "prior",
        "status",
        "salaryUnit",
        "zhineng",
        "campusSites",
        "customFields",
    ):
        value = item.get(key)
        if value not in (None, "", [], {}):
            overflow[key] = value
    return overflow
