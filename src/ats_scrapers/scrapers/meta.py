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

GraphQL listing payloads sometimes include description-like fields; when
present, the scraper carries them into ``Job.description``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import _cloakbrowser as cb
from ats_scrapers.scrapers._posting_detail import body_text, job_posting
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

_LISTING_URL = "https://www.metacareers.com/jobs"

# How long to keep listening for GraphQL responses after the listing
# page finishes its initial load. The page lazy-fires more queries as
# you scroll; we don't bother scrolling, so this just buys time for
# the first wave to settle.
_GRAPHQL_SETTLE_MS = 8_000
_DETAIL_CONCURRENCY = 4


@ScraperRegistry.register(ATSType.META)
class MetaScraper(BaseScraper):
    """Meta scraper. Single tenant — slug is ignored."""

    ats = ATSType.META

    async def afetch(self) -> list[Job]:
        if not cb.is_enabled():
            cb.warn_disabled("Meta")
            return []
        return await self._fetch_via_cloakbrowser()

    def get_description(self, job: Job) -> str | None:
        if not _needs_detail(job):
            return job.description
        self.enrich_descriptions([job])
        return job.description

    def enrich_descriptions(self, jobs: list[Job]) -> list[Job]:
        self._run_sync(self._enrich_detail_descriptions(jobs))
        return jobs

    async def _fetch_via_cloakbrowser(self) -> list[Job]:
        from cloakbrowser import launch_async

        proxy = cb.proxy_from_env()
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

        jobs = list(self._parse_responses(captured))
        if self.include_descriptions and jobs:
            await self._enrich_detail_descriptions(jobs)
        return jobs

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
                        description=self._description(entry),
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
        # Primary shape: job_search_with_featured_jobs(_v2).all_jobs
        for key in ("job_search_with_featured_jobs_v2", "job_search_with_featured_jobs"):
            jobs = (data.get(key) or {}).get("all_jobs") or []
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

    @staticmethod
    def _description(entry: dict[str, Any], fallback: dict[str, Any] | None = None) -> str | None:
        sections = (
            ("description", "description_plain", "descriptionPlain"),
            ("responsibilities",),
            ("qualifications", "minimum_qualifications"),
            ("preferred_qualifications",),
        )
        parts = [_section(entry, *keys) or _section(fallback or {}, *keys) for keys in sections]
        return "\n\n".join(dict.fromkeys(part for part in parts if part))[:25_000] or None

    async def _enrich_detail_descriptions(self, jobs: list[Job]) -> None:
        """Fetch Meta detail pages for descriptions missing from listing GraphQL.

        The listing query currently exposes id/title/location/team only. The
        public detail page embeds Schema.org ``JobPosting`` JSON-LD with the
        full body, so this path avoids another browser session per job.
        """
        targets = [job for job in jobs if _needs_detail(job)]
        if not targets:
            return

        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            await asyncio.gather(*(
                self._enrich_one_detail(client, sem, job)
                for job in targets
            ))

    async def _enrich_one_detail(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await client.get(str(job.url))
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        posting = job_posting(response.text)
        if not posting:
            return
        # Preserve sections from the listing when the detail omits them.
        job.description = self._description(posting, fallback=job.raw) or job.description


__all__ = ["MetaScraper"]


def _section(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        lines = value if isinstance(value, list) else [value]
        text = "\n".join(filter(None, (body_text(line) for line in lines)))
        if text:
            return text
    return ""


def _needs_detail(job: Job) -> bool:
    if not job.description:
        return True
    entry = job.raw or {}
    # A stored detail body can be reused. A listing introduction alone cannot.
    return job.description == MetaScraper._description(entry) and not (
        _section(entry, "description", "description_plain", "descriptionPlain")
        and _section(entry, "responsibilities")
        and (_section(entry, "qualifications") or (
            _section(entry, "minimum_qualifications") and _section(entry, "preferred_qualifications")
        ))
    )


def _description_from_detail_html(text: str) -> str | None:
    return MetaScraper._description(job_posting(text))
