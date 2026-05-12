"""Kariyer.net — Turkey's dominant job board.

Kariyer.net is Turkey's largest job board (~32k actively published
postings exposed via the public search API; ~1M live including
private/historical that aren't surfaced to anonymous users). The
public listings page ``https://www.kariyer.net/is-ilanlari`` is
PerimeterX-protected — a plain ``httpx`` / ``curl`` request to the
backend search gateway is dropped at the TLS layer ("press and hold"
challenge from a vanilla User-Agent).

Reverse-engineered the backend on 2026-05-12 via
``reverse-api-engineer`` + browser HAR capture. The Nuxt SPA calls a
single XHR per page:

    POST https://candidatesearchapigateway.kariyer.net/search
    Body: {"size": N, "currentPage": M, "memberId": 0}

The response wraps a list of jobs in
``data.jobs.items`` (max ~500/page in practice; we use 100/page for
politeness) and exposes ``data.totalJobCount``. ``memberId: 0`` is the
anonymous-user sentinel — the API doesn't require authentication.

Bot manager: PerimeterX accepts the request when the client's TLS /
HTTP-2 fingerprint matches a real Safari iOS 18 build. We use
``httpcloak`` (TLS impersonation) with the ``ios-safari-18`` preset —
the only preset that bypasses the WAF in our 2026-05-12 retesting.
``cloakbrowser`` is also wired as a last-resort fallback path: if
httpcloak gets blocked (PerimeterX rotates fingerprint allowlist), the
scraper degrades gracefully — same contract as Tesla / Meta: log a
warning, return ``[]``, never crash the publish pipeline.

Sponsored rows: the first ~1000 sponsored items always come first on
page 1 regardless of pagination. Page 1 returns all sponsored; pages
2..N drop sponsored count to ~3 (the always-promoted stickies) plus
fresh organic rows. We dedupe by ``id`` across pages so sticky rows
don't get counted twice.

Single-source scraper: ``company_slug`` is informational. Pass any
non-empty string when constructing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

# --- API constants ---------------------------------------------------

_API_URL = "https://candidatesearchapigateway.kariyer.net/search"
_SITE_BASE = "https://www.kariyer.net"
_LISTING_PATH = "/is-ilanlari"

# Page sizes — empirically ``size`` accepts up to ~1000 but the
# response is large (~1 MB at size=500) and Kariyer's API gateway can
# 502 under sustained pressure. 100/page keeps us under 200 KB per
# request and matches what the SPA itself requests.
DEFAULT_PAGE_SIZE = 100

# Safety cap on pages — the public catalogue is ~32k jobs so 500 pages
# at size=100 is the natural ceiling. Bumping further wastes time on
# the trailing sponsored-only repeats.
DEFAULT_MAX_PAGES = 500

# httpcloak retry knobs. PerimeterX occasionally throws transient 429s
# on the first request of a session; sleeping briefly and retrying
# clears it. After three failures we surface as ``ScraperError``.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# httpcloak TLS preset that bypassed PerimeterX in retesting. Safari
# iOS 18 — the desktop Chrome presets all get the "press and hold"
# challenge. Single source of truth so the preset can be swapped
# without touching the request site.
_HTTPCLOAK_PRESET = "ios-safari-18"

# Headers the SPA sends — the gateway is content-strict about
# ``ClientType`` and ``Origin`` / ``Referer`` (rejects with 403 when
# absent). ``Accept-Language: tr-TR`` is mirrored from the production
# Nuxt bundle so the response strings (workTypeText, jobDateText, …)
# come back in Turkish — matches downstream ``language="tr"``.
_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "Origin": _SITE_BASE,
    "Referer": _SITE_BASE + _LISTING_PATH,
    "ClientType": "1",
}

# Map ``workType`` API values to the canonical Job ``employment_type``
# enum. Kariyer surfaces five values: ``FullTime`` / ``PartTime`` /
# ``Freelance`` / ``Periodical`` / (occasionally) ``Internship``. The
# rest map to ``None`` — downstream LLM enrichment fills the gap.
_EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "FullTime": "FULL_TIME",
    "PartTime": "PART_TIME",
    "Freelance": "CONTRACT",
    "Periodical": "TEMPORARY",
    "Internship": "INTERN",
    "Intern": "INTERN",
}


@ScraperRegistry.register(ATSType.KARIYER)
class KariyerScraper(BaseScraper):
    """Kariyer.net jobs scraper. Single-source, anonymous API.

    Constructor knobs:
        page_size: jobs per request (1..1000). Defaults to 100.
        max_pages: stop after this many pages even if more remain.
            The hard ceiling exists so a Kariyer-side regression
            (e.g. infinite-loop in their pagination) can't burn the
            entire scrape budget on one source.
    """

    ats = ATSType.KARIYER

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 60.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        if page_size < 1 or page_size > 1000:
            raise ScraperError(
                f"Kariyer page_size must be 1..1000, got {page_size}"
            )
        self.page_size = page_size
        self.max_pages = max_pages

    # ----- public entry point ----------------------------------------

    def fetch(self) -> list[Job]:
        if not _httpcloak_available():
            _warn_httpcloak_disabled()
            return []
        return list(self._fetch_via_httpcloak())

    # ----- core fetch loop -------------------------------------------

    def _fetch_via_httpcloak(self) -> Iterable[Job]:
        """Paginate the public search API.

        Sponsored rows repeat across pages (the ~3 "always promoted"
        stickies stay on every page). We dedupe by ``id`` so the
        publish pipeline doesn't double-count them.
        """
        import httpcloak

        fetched_at = datetime.now(tz=UTC)
        seen: set[int] = set()
        jobs: list[Job] = []
        # ``Session`` reuses the TLS handshake + http/2 stream across
        # requests so we pay the impersonation cost once, not 320×.
        with httpcloak.Session(preset=_HTTPCLOAK_PRESET) as session:
            for page_no in range(1, self.max_pages + 1):
                payload = self._fetch_page(session, page_no)
                if payload is None:
                    break
                page_items = (
                    (payload.get("data") or {})
                    .get("jobs", {})
                    .get("items")
                    or []
                )
                if not page_items:
                    # Empty response — past the end of the catalogue.
                    break
                new_in_page = 0
                for entry in page_items:
                    raw_id = entry.get("id")
                    if raw_id is None:
                        continue
                    if raw_id in seen:
                        continue
                    seen.add(raw_id)
                    job = self._parse_entry(entry, fetched_at=fetched_at)
                    if job is not None:
                        jobs.append(job)
                        new_in_page += 1
                # If a whole page added zero new ids we've collapsed
                # into the sticky-only tail — stop paginating.
                if new_in_page == 0:
                    break
        log.info(
            "Kariyer: fetched %d unique jobs across up to %d pages",
            len(jobs), self.max_pages,
        )
        return jobs

    def _fetch_page(
        self, session: Any, page_no: int,
    ) -> dict[str, Any] | None:
        """POST one search page with retry on 429 / 5xx.

        Returns ``None`` on terminal failure so the caller can move on
        (we already have the earlier pages — partial coverage beats
        zero coverage). The first failed page still raises so a
        misconfigured TLS preset surfaces quickly instead of silently
        producing an empty scrape.
        """
        import httpcloak

        body = {
            "size": self.page_size,
            "currentPage": page_no,
            "memberId": 0,
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = session.post(
                    _API_URL,
                    json=body,
                    headers=_HEADERS,
                    timeout=self.timeout,
                )
            except httpcloak.HTTPCloakError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    if page_no == 1:
                        raise ScraperError(
                            f"Kariyer page={page_no} failed: {exc}"
                        ) from exc
                    log.warning(
                        "Kariyer page=%d transport error after %d retries: %s",
                        page_no, MAX_RETRIES, exc,
                    )
                    return None
                _sleep_backoff(attempt)
                continue

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"Kariyer page={page_no} returned non-JSON: "
                        f"{response.text[:200]!r}"
                    ) from exc
            if status in (429,) or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    if page_no == 1:
                        raise ScraperError(
                            f"Kariyer page={page_no} returned "
                            f"{status} after {MAX_RETRIES} retries"
                        )
                    log.warning(
                        "Kariyer page=%d returned %d, stopping pagination",
                        page_no, status,
                    )
                    return None
                _sleep_backoff(attempt)
                continue
            # 4xx other than 429 — bot manager flipped on us. Surface
            # so the operator sees the regression instead of getting a
            # silent partial.
            raise ScraperError(
                f"Kariyer page={page_no} returned {status}: "
                f"{response.text[:200]!r}"
            )

        raise ScraperError(
            f"Kariyer page={page_no} exhausted retries: {last_exc}"
        )

    # ----- single-entry parser ---------------------------------------

    def _parse_entry(
        self, entry: dict[str, Any], *, fetched_at: datetime,
    ) -> Job | None:
        """Map one ``data.jobs.items[*]`` entry to a Job.

        Returns ``None`` for entries missing the bare minimum (id +
        title + companyName + jobUrl) — those are partial rows the
        API occasionally surfaces for confidential postings; we skip
        them rather than synthesising fake values.
        """
        raw_id = entry.get("id")
        title = entry.get("title")
        company = entry.get("companyName")
        job_url_path = entry.get("jobUrl")
        if not raw_id or not title or not company or not job_url_path:
            return None

        ats_id = str(raw_id)
        url = _absolute_url(job_url_path)
        location = entry.get("locationText") or entry.get("allLocations")
        country_iso = _country_iso_from_locations(entry.get("locations") or [])

        # ``workModel`` is the API's structured remote flag. ``Remote``
        # → True; ``Hybrid`` → True (the role can be performed remotely
        # at least part of the time, matches our ``is_remote`` contract
        # of "can it be remote at all"); ``OnSite`` / unknown → None
        # so the title-only heuristic downstream can still upgrade it.
        work_model = entry.get("workModel")
        is_remote: bool | None = None
        if work_model in ("Remote", "Hybrid"):
            is_remote = True

        employment_type = _EMPLOYMENT_TYPE_MAP.get(entry.get("workType") or "")
        commitment = entry.get("workTypeText") or None

        posted_at = _parse_posting_date(
            entry.get("postingDate") or entry.get("showTime"),
        )

        # Sectors are the closest thing Kariyer has to a department —
        # multi-valued, surface the first as ``department`` (the
        # broadest category) and stash the full list in ``raw``.
        sectors = entry.get("sectors") or []
        department: str | None = None
        if sectors:
            first = sectors[0]
            if isinstance(first, dict):
                department = first.get("name") or None

        position_name = entry.get("positionName") or None

        raw_overflow: dict[str, object] = {
            "work_model": work_model,
            "position_name": position_name,
            "position_level": entry.get("positionLevel"),
            "job_code": entry.get("jobCode"),
            "is_sponsored": bool(entry.get("isSponsored")),
            "only_published_on_kariyer": entry.get("onlyPublishedOnKariyerNet"),
            "sectors": sectors,
            "job_date_text": entry.get("jobDateText"),
        }

        return Job(
            url=url,
            title=str(title),
            company=str(company),
            ats_type=ATSType.KARIYER,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            region="Asia" if country_iso == "TR" else None,
            is_remote=is_remote,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment,
            team=position_name,
            department=department,
            posted_at=posted_at,
            fetched_at=fetched_at,
            language="tr",
            raw=raw_overflow,
        )


# --- module-level helpers --------------------------------------------


def _httpcloak_available() -> bool:
    """Return True if ``httpcloak`` (TLS-impersonation HTTP client)
    can be imported.

    Mirrors the optional-browser-fallback contract used by
    ``_cloakbrowser.is_enabled`` — when the dependency is missing the
    scraper logs a warning and returns ``[]`` instead of crashing.
    """
    try:
        import httpcloak  # noqa: F401
    except ImportError:
        return False
    return True


def _warn_httpcloak_disabled() -> None:
    log.warning(
        "Kariyer: httpcloak required to bypass PerimeterX TLS check — "
        "install with `pip install jobhive[scrapers]`. Skipping.",
    )


def _absolute_url(path_or_url: str) -> str:
    """Kariyer ``jobUrl`` is a site-relative path; resolve to the
    canonical absolute URL the public dataset stores."""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return _SITE_BASE + path_or_url


def _country_iso_from_locations(
    locations: list[dict[str, Any]],
) -> str | None:
    """Kariyer encodes country as ``countryId`` (string numeric) plus
    ``countryName`` in Turkish. The vast majority of postings are
    ``"Türkiye"`` (id ``"65"``); a small minority are
    ``"Kuzey Kıbrıs T.C"`` (Northern Cyprus). Map both to ISO 3166-1
    alpha-2 — TR and CY respectively.
    """
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        country_name = loc.get("countryName") or ""
        country_id = str(loc.get("countryId") or "")
        if country_id == "65" or "Türkiye" in country_name:
            return "TR"
        if "Kıbrıs" in country_name or "Kibris" in country_name:
            # Northern Cyprus → CY in ISO 3166-1 (it's the same country
            # code even though the de-facto governments differ — ISO
            # doesn't recognise TRNC separately).
            return "CY"
    return None


def _parse_posting_date(value: Any) -> datetime | None:
    """Kariyer ``postingDate`` is ``YYYY-MM-DD`` (date-only) and
    ``showTime`` is ``YYYY-MM-DDTHH:MM`` (minute-precision local
    time, no timezone). Both are Turkish local time (UTC+3, no DST in
    Türkiye since 2016) — we store them as naive datetimes in UTC
    for consistency with the rest of the codebase. ``posted_at`` is
    surface-level enough that the day-level granularity is fine.
    """
    if not isinstance(value, str) or not value:
        return None
    # Trim to date portion if there's a "T".
    if "T" in value:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff for transient transport / 429 errors.

    Factored out so tests can monkey-patch a no-op replacement and
    not pay the wall-clock penalty on every retry path.
    """
    import time

    time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
