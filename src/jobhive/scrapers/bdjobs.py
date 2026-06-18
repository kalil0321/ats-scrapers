"""Bdjobs (https://www.bdjobs.com) — Bangladesh's largest job portal.

Bdjobs is the dominant job board in Bangladesh (170M+ population) with
~25k active vacancies across thousands of postings spanning every
sector (private, NGO, government, banking, garments, IT). The May 2026
audit had Bangladesh at near-zero coverage — this scraper closes a
huge gap in South-Asia representation.

Public REST API at ``https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch``
(GET; no auth, no key). The endpoint is a "premium spotlight" feed —
each request returns up to ten freshly-listed premium postings plus
``common.total_records_found`` / ``total_vacancies`` totals describing
the full live board (which sits behind a separate logged-in SPA we
can't hit unauthenticated).

To widen coverage beyond the unfiltered top ten we issue the same
endpoint under a curated set of ``Keyword`` and ``Category`` filters
(verified live 2026-05-12: a single ``Keyword=engineer`` query has its
own ten-row spotlight independent of the base feed). Results across
queries are deduped by ``Jobid``. This pattern matches the
``jobsch.py`` seed-segmentation approach.

Job detail page lives at the legacy ASP URL pattern documented in
``robots.txt`` and the sitemap:
``https://jobs.bdjobs.com/jobdetails.asp?id={Jobid}&ln={JobLang}``
(the SPA redirects this to its hashed ``/jobdetails/`` route but the
ASP form is the stable canonical link).

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch"
DETAIL_URL_TEMPLATE = "https://jobs.bdjobs.com/jobdetails.asp?id={job_id}&ln={lang}"

MAX_CONCURRENCY = 3
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Keyword seeds — each yields its own ten-row premium spotlight, mostly
# disjoint from the unfiltered base feed. Covers the dominant Bangladesh
# job categories (private sector white-collar, banking, IT, garments/RMG,
# NGO, healthcare, education, hospitality). All values are English; the
# Bdjobs search engine accepts English query strings against
# bilingually-tagged postings.
_KEYWORD_SEEDS: tuple[str, ...] = (
    # White-collar private sector — the long tail of Bdjobs.
    "manager", "executive", "officer", "assistant", "coordinator",
    "supervisor", "consultant", "specialist", "analyst", "director",
    # Sales / marketing / customer-facing.
    "sales", "marketing", "customer", "business development",
    "merchandiser", "retail",
    # Finance / accounting / banking (Bangladesh has a large bank sector).
    "accountant", "finance", "audit", "banking",
    # IT / engineering (smaller in absolute terms but high-signal).
    "engineer", "developer", "software", "system", "network",
    # Garments / textiles (RMG is ~80% of Bangladesh exports).
    "garments", "textile", "production", "quality",
    # NGO / aid sector (Bangladesh hosts hundreds of NGOs).
    "ngo", "project", "program",
    # Healthcare / education.
    "nurse", "doctor", "teacher", "lecturer",
    # Operations / admin.
    "admin", "hr", "operations", "logistics",
)

# Category IDs the Bdjobs search engine recognises on ``Category`` —
# these are functional-category IDs (NOT industry IDs). Probed live
# 2026-05-12: IDs 1..30 all return non-empty totals. The seed-segmentation
# pattern means we union over both keywords AND categories to maximise
# unique-id coverage.
_CATEGORY_IDS: tuple[int, ...] = tuple(range(1, 31))

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@ScraperRegistry.register(ATSType.BDJOBS)
class BdjobsScraper(BaseScraper):
    """Bdjobs (bdjobs.com) — Bangladesh's largest job board.

    Single-source: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"bangladesh"``) — the scraper enumerates the
    entire site's premium spotlight feed across a curated set of
    keyword and category seeds.

    Knobs:
    - ``keyword_seeds`` / ``category_ids`` — override the default seed
      lists. Pass ``()`` to either to disable that axis. Pass
      ``keyword_seeds=()`` AND ``category_ids=()`` to fetch only the
      unfiltered top ten.
    """

    ats = ATSType.BDJOBS

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        keyword_seeds: tuple[str, ...] | None = None,
        category_ids: tuple[int, ...] | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        # ``None`` keeps the production defaults; explicit ``()`` disables.
        self.keyword_seeds: tuple[str, ...] = (
            _KEYWORD_SEEDS if keyword_seeds is None else tuple(keyword_seeds)
        )
        self.category_ids: tuple[int, ...] = (
            _CATEGORY_IDS if category_ids is None else tuple(category_ids)
        )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True,
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            # Base feed — the unfiltered top ten premium postings.
            base = await self._fetch_query(client, sem, params=None)
            self._absorb(base, seen, jobs)
            log.info(
                "bdjobs: base feed → %d rows (total %d)",
                len(base), len(jobs),
            )

            # Seed-segmented widening — each keyword + category yields
            # its own (mostly disjoint) ten-row spotlight. Run them
            # concurrently behind ``MAX_CONCURRENCY`` so a full sweep
            # over ~60 seeds completes in well under a minute.
            queries: list[dict[str, str]] = [
                {"Keyword": kw} for kw in self.keyword_seeds
            ] + [{"Category": str(cid)} for cid in self.category_ids]

            async def one(params: dict[str, str]) -> list[dict[str, Any]]:
                try:
                    return await self._fetch_query(client, sem, params=params)
                except ScraperError as exc:
                    log.warning(
                        "bdjobs: query %s failed: %s — skipping seed", params, exc,
                    )
                    return []

            slices = await asyncio.gather(*(one(p) for p in queries))
            for items in slices:
                self._absorb(items, seen, jobs)

        log.info("bdjobs: total unique jobs → %d", len(jobs))
        return jobs

    def _absorb(
        self,
        items: list[dict[str, Any]],
        seen: set[str],
        jobs: list[Job],
    ) -> None:
        for item in items:
            job = self._parse(item)
            if job is None or job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            jobs.append(job)

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_query(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, str] | None,
    ) -> list[dict[str, Any]]:
        """Fetch all ``GetJobSearch`` pages for one query seed.

        The endpoint reports pagination under ``common.totalpages`` and
        uses ``pg`` as the page parameter. Each page splits rows between
        ``premiumData`` and ``data`` arrays; both shapes share fields.
        """
        items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            page_params = dict(params or {})
            if page > 1:
                page_params["pg"] = str(page)
            payload = await self._fetch_payload(
                client, sem, params=page_params or None,
            )
            premium = payload.get("premiumData") or []
            data = payload.get("data") or []
            if not isinstance(premium, list) or not isinstance(data, list):
                raise ScraperError(
                    f"bdjobs returned malformed job arrays for params={page_params}"
                )
            items.extend([*premium, *data])
            common = payload.get("common") or {}
            if isinstance(common, dict):
                reported_pages = _to_int(common.get("totalpages"))
                if reported_pages is not None and reported_pages > total_pages:
                    total_pages = reported_pages
            page += 1
        return items

    async def _fetch_payload(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        params: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Fetch one ``GetJobSearch`` page and return its JSON object."""
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with sem:
                    response = await client.get(
                        API_URL, params=params or {}, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                            "Origin": "https://jobs.bdjobs.com",
                        },
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"bdjobs fetch failed for params={params}: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                continue
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"bdjobs returned non-JSON for params={params}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ScraperError(
                        f"bdjobs returned malformed JSON for params={params}"
                    )
                return payload
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"bdjobs returned {response.status_code} for "
                        f"params={params} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"bdjobs returned {response.status_code} for params={params}"
            )
        raise ScraperError(
            f"bdjobs exhausted retries for params={params}: {last_exc}"
        )

    # --- parsing ------------------------------------------------------------

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("Jobid") or "").strip()
        title = (item.get("jobTitle") or "").strip()
        company = (item.get("companyName") or "").strip()
        if not ats_id or not title:
            return None

        # ``JobLang`` is the response-side language flag: ``"1"`` =
        # English listing, ``"2"`` = Bengali. Map to ISO-639-1.
        lang_raw = str(item.get("JobLang") or "1").strip()
        language = "bn" if lang_raw == "2" else "en"

        url = DETAIL_URL_TEMPLATE.format(job_id=ats_id, lang=lang_raw or "1")

        location = (item.get("location") or "").strip() or None

        description = _concat_description(item)

        posted_at = _parse_iso(item.get("publishDate"))

        raw: dict[str, Any] = {}
        for source_key, raw_key in (
            ("experience", "experience"),
            ("eduRec", "education"),
            ("jobContext", "context"),
            ("AdType", "ad_type"),
            ("deadlineDB", "deadline"),
            ("standout", "standout"),
            ("OnlineJob", "online_job"),
            ("isEarlyAccess", "early_access"),
            ("logoUrl", "logo_url"),
            ("JobTitleBng", "title_bn"),
        ):
            value = item.get(source_key)
            if value not in (None, "", []):
                raw[raw_key] = value

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.BDJOBS,
            ats_id=ats_id,
            location=location,
            country_iso="BD",
            language=language,
            description=description,
            posted_at=posted_at,
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )



def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None

def _concat_description(item: dict[str, Any]) -> str | None:
    """Bdjobs splits posting prose across ``jobContext`` (about the
    role) and ``jobDescription`` (qualifications + responsibilities).
    Concatenate the populated ones into a single body and strip the
    HTML the API ships verbatim."""
    parts: list[str] = []
    for key in ("jobContext", "jobDescription", "eduRec"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if not parts:
        return None
    body = "\n\n".join(parts)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)
    body = _WS_RE.sub(" ", body).strip()
    return body[:10_000] or None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
