"""Recruiterbox / Trakstar Hire careers scraper.

Recruiterbox (rebranded Trakstar Hire) exposes a public, unauthenticated
job-feed endpoint that does not require an API key:

    GET https://jsapi.recruiterbox.com/v1/openings?client_name={slug}&offset={n}&limit=100

Returns ``{"meta": {"offset", "limit", "total"}, "objects": [...]}`` — paginated
on the server side. We exhaust pagination internally.

Each object carries: ``id``, ``title``, ``hosted_url`` (canonical posting URL
on hire.trakstar.com), ``location`` (structured city/state/country/zipcode),
``description`` (HTML), ``allows_remote``, ``position_type``
("full_time"|...), ``team``, ``close_date``, ``client_name``.

A 400 with ``{"client_name": "Invalid client name"}`` means the slug isn't a
Recruiterbox tenant. A 200 with ``meta.total == 0`` means a valid tenant with
zero open positions.

The feed is not enough to tell that an account is open. It goes on listing
the openings of an account after the employer has left Trakstar Hire, while
the job pages themselves (``hosted_url``) answer 404 with the notice "This
employer is no longer using Trakstar Hire to collect applications". So when
the feed lists openings, one job page is read, and that notice makes the
tenant a ``CompanyNotFoundError``. A 404 without the notice is one removed
posting on an open account and is not a closed account.

An account can also be reached under an earlier name: the feed answers the
same openings, with the account's current name in ``client_name`` and in the
host of ``hosted_url``. ``company`` is taken from ``client_name``, so both
names give the same postings under the same ``(company, ats_id)``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

API_URL = "https://jsapi.recruiterbox.com/v1/openings"
PAGE_LIMIT = 100
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MAX_DESCRIPTION_LEN = 25_000

# On the job pages of an account that has been closed.
CLOSED_ACCOUNT_NOTICE = "no longer using Trakstar Hire"
_JOB_PAGE_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.hire\.trakstar\.com$", re.IGNORECASE
)

_TYPE_MAP = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "intern": "INTERN",
    "internship": "INTERN",
    "temporary": "TEMPORARY",
}


@ScraperRegistry.register(ATSType.RECRUITERBOX)
class RecruiterboxScraper(BaseScraper):
    """Recruiterbox / Trakstar Hire scraper.

    ``company_slug`` is the Recruiterbox ``client_name``
    (e.g. ``"demoaccount"`` → openings list at the API)."""

    ats = ATSType.RECRUITERBOX

    default_headers: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    async def afetch(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        async with self.make_fetcher() as fetch:
            offset = 0
            while True:
                payload = await self._fetch_page(fetch, offset)
                meta = payload.get("meta") if isinstance(payload, dict) else {}
                objects = (
                    payload.get("objects") if isinstance(payload, dict) else []
                ) or []
                for item in objects:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_opening(item)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)
                # Stop conditions: empty page, or we've reached the reported total.
                if not objects:
                    break
                total = (
                    int(meta.get("total"))
                    if isinstance(meta, dict) and isinstance(meta.get("total"), int)
                    else None
                )
                offset += len(objects)
                if total is not None:
                    if offset >= total:
                        break
                elif len(objects) < PAGE_LIMIT:
                    # Only fall back to "short page = end" when the server
                    # didn't tell us the total. Some real responses return
                    # 99 instead of 100 on a non-final page; if we have
                    # `total` we trust it.
                    break
            if await self._account_is_closed(fetch, jobs):
                raise CompanyNotFoundError(
                    f"Recruiterbox account {self.company_slug} is closed: "
                    f"its job pages say the employer no longer uses "
                    f"Trakstar Hire"
                )
        return jobs

    async def _account_is_closed(self, fetch: Fetcher, jobs: list[Job]) -> bool:
        """True when a job page carries the closed-account notice.

        One page is enough: on a closed account every job page carries it.
        """
        url = next(
            (
                str(job.url)
                for job in jobs
                if _JOB_PAGE_HOST_RE.match(urlparse(str(job.url)).hostname or "")
            ),
            None,
        )
        if url is None:
            return False
        try:
            response = await fetch.request(
                "GET",
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                handled=frozenset({404}),
            )
        except ScraperError:
            # The feed answered. A job page that cannot be read is not
            # evidence that the account is closed.
            return False
        return CLOSED_ACCOUNT_NOTICE in response.text

    async def _fetch_page(self, fetch: Fetcher, offset: int) -> dict[str, Any]:
        params = {
            "client_name": self.company_slug,
            "offset": offset,
            "limit": PAGE_LIMIT,
        }
        # A 400 with {"client_name": "Invalid client name"} means the slug
        # isn't a Recruiterbox tenant — a provider quirk the shared Fetcher
        # hands back to us unmapped.
        response = await fetch.request(
            "GET", API_URL, params=params, handled=frozenset({400})
        )
        if response.status_code == 400:
            try:
                err = response.json()
            except ValueError:
                err = {}
            if isinstance(err, dict) and "Invalid client name" in str(
                err.get("client_name", "")
            ):
                raise CompanyNotFoundError(
                    f"Recruiterbox tenant not found: {self.company_slug}"
                )
            raise ScraperError(
                f"Recruiterbox 400 for {self.company_slug}: {err or response.text[:120]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ScraperError(
                f"Recruiterbox returned malformed JSON for "
                f"{self.company_slug}: {exc}"
            ) from exc

    def _parse_opening(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        url = item.get("hosted_url") or item.get("url")
        if not ats_id or not title or not url:
            return None

        company = (
            (item.get("client_name") or "").strip() or self.company_slug
        )

        is_remote = item.get("allows_remote")
        if not isinstance(is_remote, bool):
            is_remote = None

        raw: dict[str, Any] = {}
        for k in ("position_type", "experience", "education", "industry",
                  "department", "category", "responsibilities"):
            v = item.get(k)
            if v:
                raw[k] = v

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.RECRUITERBOX,
            ats_id=ats_id,
            location=_format_location(item.get("location")),
            is_remote=is_remote,
            employment_type=_TYPE_MAP.get(
                (item.get("position_type") or "").lower()
            ),
            team=item.get("team") or None,
            commitment=item.get("position_type") if isinstance(item.get("position_type"), str) else None,
            description=_html_unescape_for_desc(item.get("description")),
            posted_at=_parse_iso(item.get("created_on") or item.get("updated_on")),
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


def _format_location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        str(value[k]).strip()
        for k in ("city", "state", "country")
        if isinstance(value.get(k), str) and value.get(k).strip()
    ]
    return ", ".join(parts) or None


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


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
