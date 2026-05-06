"""Join.com scraper.

Two-step API: resolve slug → company_id, then fetch company jobs.

    GET https://join.com/companies/{slug}        # returns metadata with id
    GET https://join.com/api/public/companies/{id}/jobs
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import CompanyNotFoundError, ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://join.com"
API_BASE = f"{BASE_URL}/api/public"


@ScraperRegistry.register(ATSType.JOIN_COM)
class JoinComScraper(BaseScraper):
    ats = ATSType.JOIN_COM

    def fetch(self) -> list[Job]:
        all_jobs: list[Job] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            company_id = self._resolve_company_id(client)
            page = 1
            while True:
                params = {
                    "locale": "en-us",
                    "page": page,
                    "pageSize": 100,
                    "withAggregations": "true",
                    "sort": "+title",
                }
                try:
                    response = client.get(
                        f"{API_BASE}/companies/{company_id}/jobs", params=params
                    )
                except httpx.HTTPError as exc:
                    raise ScraperError(
                        f"join.com jobs fetch failed for {self.company_slug}: {exc}"
                    ) from exc
                if response.status_code != 200:
                    raise ScraperError(
                        f"join.com returned {response.status_code} listing jobs for "
                        f"{self.company_slug}"
                    )
                payload = response.json()
                items = payload.get("items") or []
                all_jobs.extend(self._parse_job(item) for item in items)
                pagination = payload.get("pagination") or {}
                if page >= pagination.get("totalPages", page):
                    break
                page += 1
        return all_jobs

    def _resolve_company_id(self, client: httpx.Client) -> str:
        try:
            response = client.get(f"{BASE_URL}/companies/{self.company_slug}")
        except httpx.HTTPError as exc:
            raise ScraperError(
                f"join.com company resolve failed for {self.company_slug}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise CompanyNotFoundError(f"join.com company not found: {self.company_slug}")
        body = response.text
        # The page embeds the same Next.js page data several times. The
        # *first* numeric ``"id"`` in the body is **not** the company id
        # — that's the first department/category in the picker (e.g.
        # ``"id":233,"name":"Administration and Secretariat"``). Anchor
        # on the explicit company object instead, and *validate* that
        # the embedded ``domain`` matches the slug we asked for. join.com
        # has been observed serving template/default pages for newly-
        # created tenants where ``"company":{"id":233,...}`` (greenteg)
        # is rendered as a placeholder before the real tenant data
        # hydrates — without the domain check we would attribute
        # greenteg's jobs to whichever slug was being scraped.
        with_domain = re.search(
            r'"company"\s*:\s*\{\s*'
            r'"id"\s*:\s*"?(?P<id>\d+)"?\s*,\s*'
            r'"name"\s*:\s*"[^"]*"\s*,\s*'
            r'"domain"\s*:\s*"(?P<domain>[^"]+)"',
            body,
        )
        if with_domain:
            resolved_id = with_domain.group("id")
            resolved_domain = with_domain.group("domain").lower()
            if resolved_domain != self.company_slug.lower():
                raise CompanyNotFoundError(
                    f"join.com slug {self.company_slug!r} resolved to a "
                    f"different tenant (domain={resolved_domain!r}, "
                    f"id={resolved_id}) — likely a placeholder/cached page"
                )
            return resolved_id
        # Fallbacks for pages that don't include the domain field. These
        # don't get a domain check, so they're a last resort.
        for pattern in (
            r'"company"\s*:\s*\{\s*"id"\s*:\s*"?(\d+)"?',
            r'"companyId"\s*:\s*"?(\d+)"?',
        ):
            match = re.search(pattern, body)
            if match:
                return match.group(1)
        raise ScraperError(
            f"join.com page for {self.company_slug} did not expose a company id"
        )

    def _parse_job(self, item: dict[str, Any]) -> Job:
        raw: dict[str, Any] = {}
        for k in ("department", "category", "industry", "skills",
                  "language", "employmentType", "remoteWork", "workplaceType"):
            v = item.get(k)
            if v:
                raw[k] = v

        # join.com's API returns ``city`` as an object — pull a flat
        # ``City, Country`` label out of it. ``employmentType`` and
        # ``department`` are similarly structured; fall through to None
        # when they aren't a plain string.
        location = _flatten_location(item.get("location"), item.get("city"))
        department = _name_or_none(item.get("department"))
        employment_type = _name_or_none(item.get("employmentType"))

        # The browser-visible URL uses the slug-style ``idParam``, not the
        # numeric id (which only the API uses). Falling back to a numeric
        # path 404s.
        slug_param = item.get("idParam") or item["id"]
        url = item.get("url") or (
            f"{BASE_URL}/companies/{self.company_slug}/jobs/{slug_param}"
        )

        return Job(
            url=url,
            title=item["title"].strip(),
            company=self.company_slug,
            ats_type=ATSType.JOIN_COM,
            ats_id=str(item["id"]),
            location=location,
            department=department,
            commitment=employment_type,
            posted_at=_parse_iso(item.get("publishedAt") or item.get("createdAt")),
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _flatten_location(*values: object) -> str | None:
    """Return the first usable location label across the supplied values.

    join.com sometimes ships ``location`` as a string and sometimes only
    fills ``city`` (object: ``{"cityName": "...", "countryName": "..."}``).
    Walk both and produce a flat ``City, Country`` string."""
    for value in values:
        if not value:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        if isinstance(value, dict):
            city = (value.get("cityName") or value.get("city") or "").strip()
            country = (value.get("countryName") or value.get("country") or "").strip()
            label = ", ".join(p for p in (city, country) if p)
            if label:
                return label
    return None


def _name_or_none(value: object) -> str | None:
    """``department``/``employmentType`` may be a string or a dict with
    ``name``; only return a non-empty string."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
