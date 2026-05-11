"""Meta careers scraper — cloakbrowser-backed.

``metacareers.com`` is a single-page React app whose listing UI is fed
by GraphQL queries that require browser-issued tokens (``fb_dtsg`` and
friends). There's no public REST endpoint to call directly: the only
reliable path is to load the page in a real browser and intercept the
GraphQL responses.

``cloakbrowser`` (stealth-patched Chromium) ships its own binary and
bypasses Meta's bot-detection without a paid browser-as-a-service.
When the package isn't installed the scraper logs a warning and
returns ``[]`` so the rest of the publish pipeline keeps moving.

Listings only — per-job descriptions would need a second pass (one
navigation per job) and aren't worth the wall-clock for v1.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from jobhive.models import ATSType, Job
from jobhive.scrapers import _cloakbrowser as cb
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

_LISTING_URL = "https://www.metacareers.com/jobs"

# How long to keep listening for GraphQL responses after the listing
# page finishes its initial load. The page lazy-fires more queries as
# you scroll; we don't bother scrolling, so this just buys time for
# the first wave to settle.
_GRAPHQL_SETTLE_MS = 8_000


@ScraperRegistry.register(ATSType.META)
class MetaScraper(BaseScraper):
    """Meta scraper. Single tenant — slug is ignored."""

    ats = ATSType.META

    def fetch(self) -> list[Job]:
        if not cb.is_enabled():
            cb.warn_disabled("Meta")
            return []
        return asyncio.run(self._fetch_via_cloakbrowser())

    async def _fetch_via_cloakbrowser(self) -> list[Job]:
        from cloakbrowser import launch_async

        proxy = cb.evomi_proxy_from_env()
        captured: list[dict[str, Any]] = []

        async def on_response(resp: Any) -> None:
            if "graphql" not in resp.url:
                return
            try:
                payload = await resp.json()
            except Exception:
                # GraphQL endpoints occasionally stream non-JSON
                # (errors, redirects). Silently skip.
                return
            captured.append(payload)

        browser = await launch_async(
            headless=True, humanize=True, proxy=proxy,
        )
        try:
            page = await browser.new_page()
            page.on("response", on_response)
            try:
                await page.goto(
                    _LISTING_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await page.wait_for_timeout(_GRAPHQL_SETTLE_MS)
            except Exception as exc:
                log.warning("Meta: page load failed (%s)", exc)
        finally:
            await browser.close()

        return list(self._parse_responses(captured))

    def _parse_responses(
        self, responses: list[dict[str, Any]]
    ) -> list[Job]:
        fetched_at = datetime.now(tz=UTC)
        seen: set[str] = set()
        jobs: list[Job] = []
        for payload in responses:
            for entry in self._iter_job_entries(payload):
                job_id = entry.get("id")
                title = entry.get("title")
                if not job_id or not title:
                    continue
                if job_id in seen:
                    continue
                seen.add(job_id)
                jobs.append(
                    Job(
                        url=f"https://www.metacareers.com/jobs/{job_id}/",
                        title=title,
                        company="Meta",
                        ats_type=ATSType.META,
                        ats_id=str(job_id),
                        location=self._format_locations(entry.get("locations")),
                        team=self._first(entry.get("teams")),
                        department=self._first(entry.get("sub_teams")),
                        fetched_at=fetched_at,
                        raw=entry,
                    )
                )
        return jobs

    @staticmethod
    def _iter_job_entries(payload: dict[str, Any]):
        """Yield job dicts from the various GraphQL response shapes Meta
        has shipped. The site's queries change names without a public
        contract, so we tolerate a few aliases.
        """
        data = payload.get("data") or {}
        # Primary shape (as of 2026-05): job_search_with_featured_jobs.all_jobs
        jobs = (data.get("job_search_with_featured_jobs") or {}).get("all_jobs") or []
        if jobs:
            yield from jobs
            return
        # Fallback shapes seen in older responses or A/B variants.
        for key in ("job_search_results", "jobSearchResults"):
            results = (data.get(key) or {}).get("results") or []
            if results:
                yield from results
                return
        careers_jobs = (data.get("careers") or {}).get("jobs") or []
        yield from careers_jobs

    @staticmethod
    def _format_locations(value: Any) -> str | None:
        if not value:
            return None
        if isinstance(value, list):
            names = [v for v in value if isinstance(v, str)]
            return ", ".join(names) if names else None
        if isinstance(value, str):
            return value
        return None

    @staticmethod
    def _first(value: Any) -> str | None:
        if isinstance(value, list) and value:
            first = value[0]
            return first if isinstance(first, str) else None
        if isinstance(value, str):
            return value
        return None


__all__ = ["MetaScraper"]
