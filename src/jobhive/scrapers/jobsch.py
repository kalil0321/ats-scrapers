"""jobs.ch — Switzerland's largest direct-posting job board (~50k active).

Companies pay to list on jobs.ch — postings are not syndicated from
LinkedIn / Indeed. Coverage spans all of Switzerland (DE-CH, FR-CH,
IT-CH, EN) across every sector (the API doesn't restrict to tech).
The May 2026 audit had Switzerland at 0.2% of the dataset; this is
roughly a 25× lift.

Public REST API at ``https://www.jobs.ch/api/v1/public/search`` — no
auth, no key. Pagination is ``?start=N&rows=20`` (rows hard-capped
at 20; >20 → 422). Each entry has ``company_name`` embedded so no
separate company-resolution fetch is needed. The detail-page URL
template is in ``_links.detail_*`` (German is the canonical default).

Two anti-bot quirks to handle:

  - Datacenter IP geo-fence — bare httpx from a Hetzner / AWS /
    DigitalOcean machine returns 403 with a 919-byte block page
    (verified 2026-05-09 from Hetzner). The scraper tries direct
    first and, on 403, falls back to a residential proxy pulled
    from the ``PROXY`` env var (Evomi 4-colon shape
    ``http://host:port:user:pass``, matching Tesla / Meta).
    Without ``PROXY`` set we raise a clear error rather than
    silently 0-scraping.

  - Deep-pagination cap — ``start>=2000`` always returns 422
    regardless of IP. The empty-query view therefore tops out at
    2 000 of the ~49 000 live postings. To recover the rest we
    issue the same paginated search under ~35 keyword seeds (the
    only filter param the API honours; verified 2026-05-09:
    ``industry_ids`` / ``region_ids`` / ``place`` are all silently
    ignored — total stays at 48 892 — but ``query=developer``
    drops it to 707). Seeds span DE / FR / IT / EN to match the
    four official languages of Swiss job postings; results across
    seeds are deduped by ``job_id``.

Single-source scraper: ``company_slug`` is informational and ignored.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://www.jobs.ch/api/v1/public/search"
PER_PAGE = 20  # API hard-caps ``rows`` at 20 (>20 → 422).
MAX_CONCURRENCY = 4
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5
DETAIL_CONCURRENCY = 4
# The API hard-caps deep pagination at start=2000 (==page 100). Any
# per-query fetch therefore tops out at 2 000 rows.
MAX_USABLE_OFFSET = 2000
# Default cap on pages per query — 100 × 20 rows = the API's per-query
# ceiling. Lower via ``max_pages`` for quick smoke runs.
DEFAULT_MAX_PAGES = 100

_TAG_RE = re.compile(r"<[^>]+>")
_META_TAG_RE = re.compile(r"<meta\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_DESCRIPTION_BLOCK_RE = re.compile(
    r'<(?:div|section|article)[^>]+class=["\'][^"\']*(?:job-description|vacancy-description|description|jobad)[^"\']*["\'][^>]*>(?P<body>.*?)</(?:div|section|article)>',
    re.IGNORECASE | re.DOTALL,
)

# Keyword seeds covering DE / FR / IT / EN to defeat the per-query
# 2 000-row pagination cap. Probed live 2026-05-09: each seed below
# returns at least 36 unique-to-the-seed hits, the broad seeds
# (``a``, ``manager``, ``verkauf``) hit the 2 000 cap and contribute
# their newest 2 000 each. After dedup by ``job_id`` we observed
# ~30 k unique rows across these seeds vs the 2 000 you get with no
# query.
_QUERY_SEEDS: tuple[str, ...] = (
    # English
    "developer", "manager", "engineer", "sales", "marketing", "finance",
    "designer", "analyst", "consultant", "support", "operations", "hr",
    "lead", "senior", "junior", "intern", "specialist", "executive",
    # German
    "entwickler", "verkauf", "ingenieur", "buchhaltung", "leiter",
    "projektleiter", "kundenberater", "fachkraft", "assistent", "praktikant",
    # French
    "développeur", "responsable", "vente", "ingénieur", "comptable",
    "stagiaire", "assistant", "directeur",
    # Italian
    "sviluppatore", "vendita", "ingegnere", "responsabile",
)


class _BlockedError(Exception):
    """Internal marker — the API returned 403, retry the whole fetch
    via the residential-proxy fallback. Not raised at the public
    boundary."""


@ScraperRegistry.register(ATSType.JOBSCH)
class JobsChScraper(BaseScraper):
    """jobs.ch (Switzerland) — direct-posting board.

    Single-source: ``company_slug`` is ignored.

    Knobs:
    - ``max_pages`` — pagination cap (default 2,500, ~50k jobs).
    """

    ats = ATSType.JOBSCH

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        query_seeds: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages
        # ``None`` keeps the production default (full seed list); pass
        # ``()`` to disable seed-segmentation entirely (unit tests, or
        # callers who only want the most-recent 2 000 rows).
        self.query_seeds: tuple[str, ...] = (
            _QUERY_SEEDS if query_seeds is None else tuple(query_seeds)
        )

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    def get_description(self, job: Job) -> str | None:
        if job.description:
            return job.description
        copy = job.model_copy()
        proxy_url = _evomi_proxy_url_from_env()

        async def run() -> str | None:
            client_kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if proxy_url is not None:
                client_kwargs["proxy"] = proxy_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                sem = asyncio.Semaphore(1)
                await self._enrich_description(client, sem, copy)
            return copy.description

        return asyncio.run(run())

    async def _fetch_async(self) -> list[Job]:
        # Probe IP routing on the empty query: if the datacenter IP is
        # 403-blocked, the same probe-then-proxy escalation runs once,
        # and every subsequent seed reuses the resulting proxy_url.
        try:
            return await self._fetch_all_seeds(proxy_url=None)
        except _BlockedError:
            pass

        proxy_url = _evomi_proxy_url_from_env()
        if proxy_url is None:
            raise ScraperError(
                "jobs.ch returned 403 (likely datacenter IP block) and "
                "no PROXY env var is set. Set PROXY=http://host:port:user:pass "
                "to a residential proxy (Evomi or similar) to enable the "
                "fallback path."
            )
        log.info(
            "jobs.ch: direct request 403'd — retrying via PROXY "
            "residential fallback."
        )
        return await self._fetch_all_seeds(proxy_url=proxy_url)

    async def _fetch_all_seeds(self, *, proxy_url: str | None) -> list[Job]:
        """Run the empty-query fetch followed by every keyword seed,
        deduping by ``job_id`` across all queries.

        A 403 on the empty query escalates to the caller (proxy
        fallback). Once that's been handled (or direct works), 403s on
        individual seeds are demoted to a warning + skip — the rest of
        the seeds still run and contribute their unique rows.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []

        # Empty-query first — its 403 is the signal the caller uses
        # to flip from direct to proxy mode.
        first_slice = await self._run_fetch(proxy_url=proxy_url, query=None)
        for job in first_slice:
            if job.ats_id in seen:
                continue
            seen.add(job.ats_id)
            all_jobs.append(job)
        log.info(
            "jobs.ch: empty query → %d rows (%d new)",
            len(first_slice), len(all_jobs),
        )

        for seed in self.query_seeds:
            try:
                slice_jobs = await self._run_fetch(
                    proxy_url=proxy_url, query=seed
                )
            except _BlockedError:
                log.warning(
                    "jobs.ch: query=%s blocked even via PROXY; "
                    "skipping this seed.", seed,
                )
                continue
            new_count = 0
            for job in slice_jobs:
                if job.ats_id in seen:
                    continue
                seen.add(job.ats_id)
                all_jobs.append(job)
                new_count += 1
            log.info(
                "jobs.ch: query=%s → %d rows (%d new, total %d)",
                seed, len(slice_jobs), new_count, len(all_jobs),
            )

        if self.include_descriptions and all_jobs:
            await self._enrich_descriptions(all_jobs, proxy_url=proxy_url)
        return all_jobs

    async def _run_fetch(
        self, *, proxy_url: str | None, query: str | None
    ) -> list[Job]:
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        # After the proxy switch, any further 403 mid-pagination is a
        # per-IP rate-limit / regional dropout — drop that page so the
        # rest of the slice survives.
        already_in_proxy_mode = proxy_url is not None

        async def absorb(items: list[dict[str, Any]]) -> None:
            async with lock:
                for it in items:
                    job = self._parse(it)
                    if job is None or job.ats_id in seen:
                        continue
                    seen.add(job.ats_id)
                    jobs.append(job)

        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if proxy_url is not None:
            client_kwargs["proxy"] = proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            first = await self._fetch_page(client, sem, start=0, query=query)
            total = int(first.get("total_hits") or 0)
            await absorb(first.get("documents") or [])

            if total <= PER_PAGE:
                return jobs

            usable = min(total, MAX_USABLE_OFFSET)
            page_count = min(
                (usable + PER_PAGE - 1) // PER_PAGE, self.max_pages
            )
            offsets = [PER_PAGE * i for i in range(1, page_count)]

            async def one(offset: int) -> None:
                try:
                    payload = await self._fetch_page(
                        client, sem, start=offset, query=query
                    )
                except _BlockedError:
                    if not already_in_proxy_mode:
                        raise
                    return
                await absorb(payload.get("documents") or [])

            await asyncio.gather(*(one(o) for o in offsets))
        return jobs

    async def _enrich_descriptions(
        self, jobs: list[Job], *, proxy_url: str | None
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if proxy_url is not None:
            client_kwargs["proxy"] = proxy_url

        async with httpx.AsyncClient(**client_kwargs) as client:
            detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
            await asyncio.gather(*(
                self._enrich_description(client, detail_sem, job) for job in jobs
            ))

    async def _enrich_description(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        job: Job,
    ) -> None:
        async with sem:
            try:
                response = await client.get(
                    str(job.url),
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
                )
            except httpx.HTTPError:
                return
        if response.status_code != 200:
            return
        description = _extract_description(response.text)
        if description and not job.description:
            job.description = description[:25_000]

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        start: int,
        query: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"start": start, "rows": PER_PAGE}
        if query:
            params["query"] = query
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(
                        API_URL, params=params, headers={
                            "User-Agent": "Mozilla/5.0",
                            "Accept": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"jobs.ch fetch failed at start={start}: {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ScraperError(
                        f"jobs.ch returned non-JSON at start={start}: {exc}"
                    ) from exc
            if response.status_code == 403:
                # Datacenter IP block — escalate to ``_fetch_async`` so
                # it can retry the whole fetch through the residential
                # proxy. Don't burn retries here.
                raise _BlockedError(
                    f"jobs.ch returned 403 at start={start}"
                )
            if response.status_code == 422:
                # Past the search-engine cap (rare; API caps deep
                # pagination differently per query). Treat as exhausted.
                return {"documents": [], "total_hits": 0}
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"jobs.ch returned {response.status_code} at "
                        f"start={start} after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"jobs.ch returned {response.status_code} at start={start}"
            )
        raise ScraperError(
            f"jobs.ch exhausted retries at start={start}: {last_exc}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        ats_id = str(item.get("job_id") or "")
        title = (item.get("title") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not ats_id or not title:
            return None

        url = _detail_url(item, ats_id)

        # ``place`` is the city name; ``regions`` is a numeric path
        # (cantons + sub-regions) we don't have a name table for. The
        # city is enough for downstream geo-search.
        place = (item.get("place") or "").strip() or None
        location = f"{place}, Switzerland" if place else "Switzerland"

        # employment_grades is a list like [100] (% time). When the
        # only value is below 100 the role is part-time; when 100 it's
        # full-time; mixed lists indicate flexibility.
        grades = item.get("employment_grades") or []
        is_full_time = grades == [100]
        employment_type = "FULL_TIME" if is_full_time else (
            "PART_TIME" if grades and all(g < 100 for g in grades) else None
        )

        posted_at = _parse_iso(
            item.get("publication_date") or item.get("initial_publication_date")
        )

        raw: dict[str, Any] = {}
        if grades:
            raw["employment_grades"] = grades
        languages = [
            entry.get("language") for entry in (item.get("language_skills") or [])
            if isinstance(entry, dict) and entry.get("language")
        ]
        if languages:
            raw["languages"] = languages
        if item.get("company_id"):
            raw["company_id"] = str(item["company_id"])
        if item.get("company_segmentation"):
            raw["company_segmentation"] = item["company_segmentation"]

        return Job(
            url=url,
            title=title,
            company=company or "Unknown",
            ats_type=ATSType.JOBSCH,
            ats_id=ats_id,
            location=location,
            employment_type=employment_type,
            posted_at=posted_at,
            fetched_at=datetime.now(),
            raw=raw or None,
        )


def _detail_url(item: dict[str, Any], job_id: str) -> str:
    """Prefer ``_links.detail_{lang}.href`` when present (jobs.ch ships
    a localized detail URL per row), else fall back to the documented
    canonical English URL pattern.
    """
    links = item.get("_links") or {}
    if isinstance(links, dict):
        for key in ("detail_en", "detail_de", "detail_fr", "detail_it"):
            entry = links.get(key)
            if isinstance(entry, dict):
                href = entry.get("href")
                if isinstance(href, str) and href:
                    return href
    return f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_description(text: str) -> str | None:
    match = _DESCRIPTION_BLOCK_RE.search(text)
    if match:
        cleaned = _strip_html(match.group("body"))
        if cleaned:
            return cleaned
    meta = _extract_meta_description(text)
    return meta or None


def _extract_meta_description(text: str) -> str | None:
    for tag in _META_TAG_RE.finditer(text):
        attrs = {
            m.group("name").lower(): html.unescape(m.group("value"))
            for m in _ATTR_RE.finditer(tag.group("attrs"))
        }
        kind = (attrs.get("name") or attrs.get("property") or "").lower()
        if kind not in {"description", "og:description"}:
            continue
        cleaned = _strip_html(attrs.get("content") or "")
        if cleaned:
            return cleaned
    return None


def _strip_html(value: str) -> str:
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _evomi_proxy_url_from_env() -> str | None:
    """Parse the ``PROXY`` env var into an httpx-compatible proxy URL.

    Evomi ships ``PROXY`` in the 4-colon
    ``http://host:port:user:pass`` shape (same shape the
    ``_browserbase`` helper consumes for patchright). We rebuild it
    into the standard ``http://user:pass@host:port`` form that httpx
    accepts. Returns ``None`` when no env var is set so the caller can
    surface a clear error instead of silently no-op'ing.
    """
    raw = os.getenv("PROXY")
    if not raw:
        return None
    rest = raw.replace("http://", "").replace("https://", "")
    parts = rest.split(":")
    if len(parts) != 4:
        log.warning(
            "PROXY env var doesn't match host:port:user:pass shape; "
            "skipping jobs.ch fallback."
        )
        return None
    host, port, user, password = parts
    return f"http://{user}:{password}@{host}:{port}"
