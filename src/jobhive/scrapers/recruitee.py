"""Recruitee scraper.

Recruitee exposes a clean public JSON API per tenant:

    GET https://{slug}.recruitee.com/api/offers

Returns a single payload with every active offer — no pagination, full
description and requirements inline. Custom domains are also supported by
passing the bare hostname or full URL as `company_slug`.

    >>> RecruiteeScraper("monzo").fetch()
    >>> RecruiteeScraper("careers.acme.com").fetch()
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any


@ScraperRegistry.register(ATSType.RECRUITEE)
class RecruiteeScraper(BaseScraper):
    """Recruitee scraper.

    `company_slug` semantics:
      * bare slug like `"monzo"` — resolves to `https://monzo.recruitee.com`
      * full URL — used as the API host directly (custom domain support)
    """

    ats = ATSType.RECRUITEE

    def fetch(self) -> list[Job]:
        api_url = self._resolve_api_url()
        try:
            response = httpx.get(
                api_url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"Recruitee fetch failed for {self.company_slug}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"Recruitee company not found: {self.company_slug}"
            )
        if response.status_code != 200:
            raise ScraperError(
                f"Recruitee returned {response.status_code} for {self.company_slug}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ScraperError(f"Recruitee returned non-JSON: {exc}") from exc
        offers = payload.get("offers") or []
        return [self._parse_offer(o) for o in offers if isinstance(o, dict)]

    def _resolve_api_url(self) -> str:
        slug = self.company_slug.strip().rstrip("/")
        if slug.startswith(("http://", "https://")):
            base = slug
            if not base.endswith("/api/offers"):
                base = f"{base}/api/offers"
            return base
        return f"https://{slug}.recruitee.com/api/offers"

    def _parse_offer(self, offer: dict[str, Any]) -> Job:
        location = _format_location(offer)
        loc_obj = offer.get("location") if isinstance(offer.get("location"), dict) else {}

        url = offer.get("careers_url") or offer.get("careers_apply_url") or _fallback_url(self.company_slug, offer)
        apply_url = offer.get("careers_apply_url")

        is_remote = None
        if isinstance(offer.get("remote"), bool):
            is_remote = offer["remote"]

        commitment = offer.get("category") or offer.get("schedule")
        salary_obj = offer.get("salary") if isinstance(offer.get("salary"), dict) else {}

        raw: dict[str, Any] = {}
        for k in ("category", "experience", "education", "tags", "industry",
                  "function", "kind", "schedule"):
            v = offer.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=offer.get("title") or offer.get("position") or "Untitled",
            company=offer.get("company_name") or self.company_slug,
            ats_type=ATSType.RECRUITEE,
            ats_id=str(offer.get("id") or offer.get("slug") or ""),
            location=location,
            lat=_to_float(offer.get("lat") or loc_obj.get("lat")),
            lon=_to_float(offer.get("lng") or loc_obj.get("lng")),
            is_remote=is_remote,
            employment_type=_map_employment_type(offer.get("employment_type_code") or offer.get("employment_type")),
            department=offer.get("department") or offer.get("department_name"),
            commitment=commitment if isinstance(commitment, str) else None,
            apply_url=apply_url if isinstance(apply_url, str) and apply_url != url else None,
            salary_min=_to_float(salary_obj.get("min")) if salary_obj else None,
            salary_max=_to_float(salary_obj.get("max")) if salary_obj else None,
            salary_currency=salary_obj.get("currency") if salary_obj else None,
            description=_compose_description(offer),
            posted_at=_parse_iso(offer.get("created_at") or offer.get("published_at")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


_EMPLOYMENT_MAP = {
    "permanent": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "fulltime_permanent": "FULL_TIME",
    "full_time": "FULL_TIME",
    "permanent_fulltime": "FULL_TIME",
    "permanent_full_time": "FULL_TIME",
    "fixed_term": "CONTRACT",
    "temporary": "TEMPORARY",
    "contract": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "trainee": "INTERN",
    "apprentice": "INTERN",
    "part_time": "PART_TIME",
    "parttime": "PART_TIME",
    "parttime_permanent": "PART_TIME",
    "permanent_parttime": "PART_TIME",
    "permanent_part_time": "PART_TIME",
    "casual": "TEMPORARY",
    "seasonal": "TEMPORARY",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _map_employment_type(value: object) -> str | None:
    """Coerce Recruitee's ``employment_type_code`` to the canonical enum.

    Accepts both bare codes (``permanent``) and the prefixed variants
    Recruitee added in 2024 (``parttime_permanent``,
    ``fulltime_permanent``, ``fixed_term_fulltime``…).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    norm = value.lower().replace("-", "_").strip()
    if norm in _EMPLOYMENT_MAP:
        return _EMPLOYMENT_MAP[norm]
    # Substring match for tenant-specific oddities.
    for needle, mapped in _EMPLOYMENT_MAP.items():
        if needle in norm:
            return mapped
    return None


def _format_location(offer: dict[str, Any]) -> str | None:
    if isinstance(offer.get("location"), str) and offer["location"].strip():
        return offer["location"].strip()
    parts = [offer.get("city"), offer.get("state_code"), offer.get("country_code")]
    formatted = ", ".join(p for p in parts if p)
    return formatted or None


def _compose_description(offer: dict[str, Any]) -> str | None:
    """Concatenate ``description`` and ``requirements`` into a single
    plain-text body. Recruitee renders both fields as HTML; we strip
    tags + decode entities so consumers don't have to."""
    parts: list[str] = []
    for key in ("description", "requirements"):
        value = offer.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = _TAG_RE.sub(" ", value)
            cleaned = html_mod.unescape(cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned:
                parts.append(cleaned)
    if not parts:
        return None
    return "\n\n".join(parts)[:10_000]


def _fallback_url(slug: str, offer: dict[str, Any]) -> str:
    offer_slug = offer.get("slug") or offer.get("id", "")
    return f"https://{slug}.recruitee.com/o/{offer_slug}"


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    """Parse Recruitee's published_at / created_at timestamps.

    Recruitee ships dates in the ``"2025-12-05 21:44:46 UTC"`` form
    (space separator, trailing ``UTC``) — neither ISO 8601 nor the
    ``Z`` suffix. We try ISO first, then fall through to the Recruitee
    locale string.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Recruitee form: "YYYY-MM-DD HH:MM:SS UTC"
    cleaned_no_tz = re.sub(r"\s+UTC\s*$", "", cleaned)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned_no_tz, fmt)
        except ValueError:
            continue
    return None
