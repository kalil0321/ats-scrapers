"""Meta careers scraper — patchright-backed.

``metacareers.com`` is a single-page React app whose listing UI is
fed by GraphQL queries that require browser-issued tokens
(``fb_dtsg`` and friends). There's no public REST endpoint to call
directly: the only reliable path is to load the page in a real
browser and intercept the GraphQL responses.

We use `patchright`_ — a Playwright fork with stealth patches in the
bundled Chromium — so the scraper runs locally without any paid
service. Same opt-in flag as Tesla: set ``JOBHIVE_USE_BROWSERBASE=1``
to enable. Without the flag, ``fetch()`` returns ``[]`` with a single
warning so the rest of the pipeline keeps moving. Without patchright
but with the flag set, raises a clear ``ScraperError``.

.. _patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright

Two-pass scrape:

1. **Listings** — open ``/jobs``, intercept GraphQL responses, parse
   the canonical job entries (id, title, locations, teams, sub_teams)
   from ``job_search_with_featured_jobs.all_jobs``.
2. **Descriptions** — for each job, navigate to ``/jobs/{id}/`` in a
   pool of parallel tabs and capture the rendered description text.
   Best-effort: a single failed detail page is logged but doesn't
   skip the job (we still ship the listing-level row, just without
   a description).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers import _browserbase as bb
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

log = logging.getLogger(__name__)

_LISTING_URL = "https://www.metacareers.com/jobs"
_DETAIL_URL_TEMPLATE = "https://www.metacareers.com/jobs/{id}/"

# How long to keep listening for GraphQL responses after the listing
# page finishes its initial load.
_GRAPHQL_SETTLE_MS = 8_000

# Concurrent detail tabs. 6 keeps the per-tenant time tight without
# hammering Meta. Each tab is one navigation per job.
_DETAIL_CONCURRENCY = 6
_DETAIL_TIMEOUT_MS = 30_000

# The detail page lays out:
#
#   Skip to main content / Jobs / Teams / … / Jobs / {title} /
#   {title} / {location} / {team} / +N more / Apply now /
#   <DESCRIPTION> / … / footer (which itself contains another
#   "APPLY NOW" call-to-action).
#
# We grab the whole body text, find the FIRST "Apply now" (the per-
# job CTA — Meta cases it lowercase; the footer's "APPLY NOW" is
# always uppercase, so a case-sensitive match avoids the footer
# entirely), and take everything after it. The description always
# starts on the next line.
_DESCRIPTION_ANCHOR = "Apply now"

# When a job has been removed, the page renders this banner instead
# of a description. We surface ``None`` rather than ship the
# placeholder text — the listing-level row stays in the dataset,
# just without a description until the next scrape (or until LLM
# enrichment looks elsewhere).
_DELETED_JOB_MARKER = "Sorry, this job is no longer available"

# Cookie banners + the "Find your role" / per-job apply CTAs leak
# into body inner_text. We trim everything starting at the FIRST
# of these markers — they always sit AFTER the description so the
# slice is safe. Order matters: we want the earliest match.
_DESCRIPTION_FOOTER_MARKERS = (
    "Apply for this job",
    "Find your role",
    "Take the first step",
    "Cookie Policy",
    "Recruiters can view your",
    "APPLY NOW",
)


@ScraperRegistry.register(ATSType.META)
class MetaScraper(BaseScraper):
    """Meta scraper. Single tenant — slug is ignored."""

    ats = ATSType.META

    def fetch(self) -> list[Job]:
        if not bb.is_enabled():
            bb.warn_disabled("Meta")
            return []
        return asyncio.run(self._fetch_via_patchright())

    async def _fetch_via_patchright(self) -> list[Job]:
        try:
            from patchright.async_api import Response, async_playwright
        except ImportError as exc:
            raise ScraperError(
                "Meta requires `patchright` to bypass Meta's "
                "GraphQL-token gating. Install with "
                "`pip install jobhive-py[browser]`, then run "
                "`patchright install chromium` to download its "
                "bundled Chromium build."
            ) from exc

        captured_listings: list[dict[str, Any]] = []

        async def on_response(resp: Response) -> None:
            if "graphql" not in resp.url:
                return
            try:
                payload = await resp.json()
            except Exception:
                # GraphQL endpoints occasionally stream non-JSON
                # (error envelopes, redirects); skip silently.
                return
            captured_listings.append(payload)

        # Headed Chromium — keeps the launch shape identical to
        # Tesla (which actually requires it for the Akamai bypass)
        # so a single cron host setup covers both browser-required
        # scrapers. Cron runs overnight when no operator is at the
        # keyboard, so no need to hide the window.
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=False)
            except Exception as exc:
                raise ScraperError(
                    f"Meta: patchright Chromium launch failed ({exc}). "
                    "Did you run `patchright install chromium`?"
                ) from exc
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                )
                page = await ctx.new_page()
                page.on("response", on_response)
                try:
                    await page.goto(
                        _LISTING_URL,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await page.wait_for_timeout(_GRAPHQL_SETTLE_MS)
                except Exception as exc:
                    log.warning("Meta: listing page load failed (%s)", exc)

                jobs = list(self._parse_responses(captured_listings))
                if jobs:
                    await self._enrich_with_descriptions(ctx, jobs)
            finally:
                await browser.close()

        return jobs

    async def _enrich_with_descriptions(
        self,
        ctx: Any,
        jobs: list[Job],
    ) -> None:
        """Visit each job's detail URL in a pool of parallel tabs and
        attach the description text. Best-effort — a tab failure logs
        a warning and leaves the description ``None``."""
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def fetch_one(job: Job) -> None:
            async with sem:
                tab = await ctx.new_page()
                try:
                    description = await self._fetch_detail_description(
                        tab, str(job.url)
                    )
                except Exception as exc:
                    log.warning(
                        "Meta: detail fetch failed for %s (%s)",
                        job.ats_id, exc,
                    )
                    return
                finally:
                    with contextlib.suppress(Exception):
                        await tab.close()
                if description:
                    object.__setattr__(job, "description", description)

        await asyncio.gather(*(fetch_one(j) for j in jobs))

    @staticmethod
    async def _fetch_detail_description(tab: Any, url: str) -> str | None:
        try:
            await tab.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_DETAIL_TIMEOUT_MS,
            )
        except Exception:
            return None
        # Give React a beat to hydrate the description.
        await tab.wait_for_timeout(1500)
        try:
            body_text = await tab.inner_text("body")
        except Exception:
            return None
        if not body_text:
            return None
        # Removed-job placeholder: ship None so the row keeps its
        # listing-level data without lying about a description.
        if _DELETED_JOB_MARKER in body_text:
            return None
        # Slice off the page chrome — everything before the FIRST
        # "Apply now" CTA is nav / breadcrumbs / repeated title.
        # Case-sensitive match: the footer's "APPLY NOW" button is
        # uppercase and we don't want to slice there.
        anchor_at = body_text.find(_DESCRIPTION_ANCHOR)
        if anchor_at < 0:
            return None
        description = body_text[anchor_at + len(_DESCRIPTION_ANCHOR):]
        # Trim everything starting at the EARLIEST footer marker —
        # cookie banner, per-job apply CTA, "Find your role" widget.
        cuts = [
            description.find(m) for m in _DESCRIPTION_FOOTER_MARKERS
        ]
        cuts = [c for c in cuts if c >= 0]
        if cuts:
            description = description[: min(cuts)]
        description = description.strip()
        if len(description) < 80:
            return None
        # Trim runs of blank lines and cap at ~10kB to match the
        # Job.description docstring contract.
        cleaned = re.sub(r"\n{3,}", "\n\n", description)
        return cleaned[:10_000]

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
                        url=_DETAIL_URL_TEMPLATE.format(id=job_id),
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
        """Yield job dicts from the various GraphQL response shapes
        Meta has shipped. The site's queries change names without a
        public contract, so we tolerate a few aliases."""
        data = payload.get("data") or {}
        # Primary shape (as of 2026-05): job_search_with_featured_jobs.all_jobs
        jobs = (data.get("job_search_with_featured_jobs") or {}).get("all_jobs") or []
        if jobs:
            yield from jobs
            return
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
