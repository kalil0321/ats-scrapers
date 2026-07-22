"""Pinpoint (pinpointhq.com) careers scraper.

Pinpoint exposes a single public, unauthenticated JSON endpoint per tenant:

    GET https://{slug}.pinpointhq.com/postings.json

Returns ``{"data": [{"id": "...", "title": "...", "url": "...",
"location": {"city": ..., "name": ..., "province": ...},
"compensation_minimum": ..., "compensation_maximum": ...,
"compensation_currency": "USD", "compensation_frequency": "yearly",
"workplace_type": "remote"|"hybrid"|"onsite", "employment_type": "full_time"|...,
"job": {"department": {"name": ...}}}]}`` — every active posting in one
response, no pagination.

Tenants without an active Pinpoint careers site return 404. Locale variants
(``/fr/postings.json``) are supported but we always pull English.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._slug import require_host_label
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

API_TEMPLATE = "https://{slug}.pinpointhq.com/postings.json"
# Tenants without an active careers site 3xx-redirect to the marketing
# site — that's a "not found", not an error, so the shared Fetcher hands
# these statuses back to us unmapped.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 25_000

# Pinpoint sometimes prefixes the employment-type code with the
# contract status (``permanent_full_time``, ``permanent_part_time``,
# ``fixed_term_full_time``…). We collapse the prefix and then match
# against the canonical FT/PT/CONTRACT/etc. codes.
_TYPE_MAP = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "fixed_term": "CONTRACT",
    "fixed_term_full_time": "CONTRACT",
    "fixed_term_part_time": "CONTRACT",
    "freelance": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "apprentice": "INTERN",
    "apprenticeship": "INTERN",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "permanent_full_time": "FULL_TIME",
    "permanent_part_time": "PART_TIME",
    "permanent": "FULL_TIME",
}

_PERIOD_MAP = {
    "yearly": "YEAR",
    "monthly": "MONTH",
    "weekly": "WEEK",
    "daily": "DAY",
    "hourly": "HOUR",
}


@ScraperRegistry.register(ATSType.PINPOINT)
class PinpointScraper(BaseScraper):
    """Pinpoint scraper. ``company_slug`` is the tenant subdomain
    (e.g. ``"workwithus"`` → ``https://workwithus.pinpointhq.com/postings.json``)."""

    ats = ATSType.PINPOINT

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
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
        self.company_slug = require_host_label(
            company_slug, provider="PinpointScraper"
        )

    async def afetch(self) -> list[Job]:
        url = API_TEMPLATE.format(slug=self.company_slug)
        async with self.make_fetcher(follow_redirects=False) as fetch:
            response = await fetch.request("GET", url, handled=_REDIRECT_STATUSES)
        if response.status_code in _REDIRECT_STATUSES:
            raise CompanyNotFoundError(
                f"Pinpoint tenant has no active careers site: {self.company_slug}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ScraperError(
                f"Pinpoint returned malformed JSON for {self.company_slug}: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ScraperError(
                f"Pinpoint returned unexpected payload for {self.company_slug}"
            )
        seen: set[str] = set()
        jobs: list[Job] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            job = self._parse_posting(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)
        return jobs

    def _parse_posting(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        url = item.get("url")
        if not ats_id or not title or not url:
            return None

        comp_currency = item.get("compensation_currency")
        comp_min = _to_float(item.get("compensation_minimum"))
        comp_max = _to_float(item.get("compensation_maximum"))
        comp_period = _PERIOD_MAP.get(
            (item.get("compensation_frequency") or "").lower()
        )
        if not item.get("compensation_visible"):
            # Pinpoint surfaces compensation only when the recruiter has chosen
            # to make it public; otherwise the numeric fields can leak internal
            # band data. Respect the visibility flag.
            comp_min = comp_max = None
            comp_currency = None
            comp_period = None

        job_meta = item.get("job") if isinstance(item.get("job"), dict) else {}
        dept = job_meta.get("department") if isinstance(job_meta, dict) else None
        department = (
            dept.get("name") if isinstance(dept, dict) and dept.get("name") else None
        )

        raw: dict[str, Any] = {}
        for k in ("workplace_type", "experience_level", "office",
                  "schedule", "tags", "remote_country_restriction"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=title,
            company=self.company_slug,
            ats_type=ATSType.PINPOINT,
            ats_id=ats_id,
            location=_format_location(item.get("location")),
            is_remote=_extract_is_remote(item.get("workplace_type")),
            employment_type=_map_employment_type(item.get("employment_type")),
            department=department,
            commitment=item.get("schedule") if isinstance(item.get("schedule"), str) else None,
            requisition_id=item.get("reference") if isinstance(item.get("reference"), str) else None,
            description=_html_unescape_for_desc(item.get("description")),
            salary_currency=comp_currency,
            salary_min=comp_min,
            salary_max=comp_max,
            salary_period=comp_period,
            posted_at=_parse_iso(item.get("first_published_at")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _map_employment_type(value: object) -> str | None:
    """Coerce Pinpoint's freeform ``employment_type`` to the canonical enum.

    Tries the full string first (``permanent_full_time`` → FULL_TIME),
    then falls through to suffix matches (``permanent_full_time`` →
    look up ``full_time``) so unprefixed and tenant-prefixed values
    both work.
    """
    if not isinstance(value, str):
        return None
    norm = value.strip().lower()
    if not norm:
        return None
    if norm in _TYPE_MAP:
        return _TYPE_MAP[norm]
    # Try stripping known prefixes like ``permanent_`` / ``fixed_term_``.
    for prefix in ("permanent_", "fixed_term_", "regular_"):
        if norm.startswith(prefix):
            tail = norm[len(prefix):]
            if tail in _TYPE_MAP:
                return _TYPE_MAP[tail]
    # Last-resort: substring match.
    for needle, mapped in _TYPE_MAP.items():
        if needle in norm:
            return mapped
    return None


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        # `name` is the user-visible label ("Remote", "London", "London, UK").
        return name.strip()
    parts = [
        str(value[k]).strip()
        for k in ("city", "province", "country")
        if isinstance(value.get(k), str) and value.get(k).strip()
    ]
    return ", ".join(parts) or None


def _extract_is_remote(workplace_type: object) -> bool | None:
    if not isinstance(workplace_type, str):
        return None
    wt = workplace_type.strip().lower()
    if wt == "remote":
        return True
    if wt in ("onsite", "on_site", "office"):
        return False
    return None


def _html_unescape_for_desc(value: object, *, cap: int = 25_000) -> str | None:
    """Unescape HTML entities and trim/cap, but keep tags intact so the
    post-scrape markdownify pass can preserve paragraph and list structure.
    Replaces the legacy _strip_html/_html_to_text path for descriptions
    only — title/company/salary fields still use the strip variant."""
    import html as _h
    if not isinstance(value, str):
        return None
    out = _h.unescape(value).strip()
    if not out:
        return None
    return out[:cap]


def _html_to_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = HTML_TAG_RE.sub(" ", value)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    return text[:MAX_DESCRIPTION_LEN]


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
