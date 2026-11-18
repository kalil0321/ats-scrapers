"""EURES (European Employment Services) scraper.

EURES aggregates job vacancies across the 31 EU/EEA countries. The
public portal at ``europa.eu/eures`` exposes an unauthenticated JSON
API the frontend consumes:

    POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search

The API caps every query at 10,000 results (50 ``resultsPerPage`` x
200 ``page``). A query above the cap is split into narrower queries,
recursively, until every slice fits:

- Each response carries a ``facets`` block with per-value counts for
  location, contract type, experience, education, EURES flag, sector,
  schedule, required languages and benefits. Facets ignore their own
  filter and apply all the others, so the counts describe the children
  of the current slice. Every facet lists an ``NS`` (not specified)
  value, which the filters accept, so no vacancy falls outside a facet
  (languages need one more child, see ``_ISO_639_1``).
- ``_plan_split`` picks the facet that leaves the fewest vacancies above
  the cap and repeats the fewest (a vacancy can carry several regions,
  sectors or schedules). Location is a tree: countries, then the NUTS
  regions the portal indexes for that country (deeper NUTS codes match
  nothing).
- When no facet can split a slice further, the ISCO occupation tree
  (``occupationUris``) takes over. It has no facet, so its child counts
  come from the child queries themselves, and vacancies without an
  occupation fall outside it.

Values of one filter are ORed, so one extra query with every child value
counts the union of the children, which must reach the parent total. It
also catches facet values the filter does not accept. A shortfall, or a
slice that cannot be split below the cap, is logged as a warning with the
number of vacancies left out.

Sorting does not help past the cap: ``BEST_MATCH`` and ``MOST_RECENT``
return the same order when no keyword is set.

Single-source scraper: ``company_slug`` is informational and ignored.
The output rows carry the publishing employer's name as ``company`` so
the publisher's cross-ATS dedup still works.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers import fetch as fetch_mod
from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers._posting_detail import decode_encoded_tags
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from typing import Any

    from ats_scrapers.fetch import Fetcher

log = logging.getLogger(__name__)

API_URL = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
)
DETAIL_URL_FMT = (
    "https://europa.eu/eures/portal/jv-se/jv-details/{jv_id}?lang=en"
)
DETAIL_API_URL_FMT = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv/id/{jv_id}?requestLang=en&preferredLang=en"
)
PAGE_SIZE = 50  # API caps `resultsPerPage` at 50 (>50 returns 400).
# ``page`` x ``resultsPerPage`` above 10,000 returns 400, whatever the size.
PAGE_LIMIT = 200
PAGINATION_CAP = PAGE_SIZE * PAGE_LIMIT  # 10,000 jobs per query.
MAX_CONCURRENCY = 6  # The portal is generous but we stay polite.
# Slices reading their pages at once. Each keeps the ids and timestamps of
# its pages (up to 10,000 rows) until its rereads are done, and rereads
# queue behind the pages of the other active slices; enough slices to keep
# every request busy, few enough to bound that memory.
MAX_ACTIVE_SLICES = 2 * MAX_CONCURRENCY
_REREAD_ROUNDS = 4  # See ``_rereads``.


@dataclass(frozen=True)
class _Dimension:
    name: str
    facet: str | None
    field: str


_LOCATION = _Dimension("location", "POSITION_LOCATION", "locationCodes")
_EDUCATION = _Dimension(
    "education",
    "EDUCATION_AND_QUALIFICATION_LEVEL",
    "educationAndQualificationLevelCodes",
)
_LANGUAGE = _Dimension("language", "LANGUAGE_SKILLS", "requiredLanguages")
_OCCUPATION = _Dimension("occupation", None, "occupationUris")
_FACET_DIMENSIONS = (
    _Dimension("contract", "POSITION_OFFERING", "positionOfferingCodes"),
    _Dimension("experience", "REQUIRED_EXPERIENCE", "requiredExperienceCodes"),
    _EDUCATION,
    _Dimension("flag", "EURES_FLAG", "euresFlagCodes"),
    _Dimension("sector", "NACE_CODE", "sectorCodes"),
    _Dimension("schedule", "POSITION_SCHEDULE", "positionScheduleCodes"),
    _LANGUAGE,
    _Dimension("benefit", "EXTRA_BENEFIT", "otherBenefitsCodes"),
)

# The language filter takes {isoCode, level} and matches vacancies that
# require the language at or below that CEFR level, so C2 matches them all.
# Any other level string fails with a server error.
_LANGUAGE_ANY_LEVEL = "C2"

# The language facet lists only the EU/EEA languages, but the filter takes
# any ISO 639-1 code, and some vacancies require only others (Russian,
# Luxembourgish, Ukrainian, ...). One extra child ORs every code the facet
# does not list.
_ISO_639_1 = (
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az", "ba",
    "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs", "ca", "ce", "ch", "co",
    "cr", "cs", "cu", "cv", "cy", "da", "de", "dv", "dz", "ee", "el", "en", "eo",
    "es", "et", "eu", "fa", "ff", "fi", "fj", "fo", "fr", "fy", "ga", "gd", "gl",
    "gn", "gu", "gv", "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz", "ia",
    "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu", "ja", "jv", "ka", "kg",
    "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv", "mg", "mh", "mi", "mk",
    "ml", "mn", "mr", "ms", "mt", "my", "na", "nb", "nd", "ne", "ng", "nl", "nn",
    "no", "nr", "nv", "ny", "oc", "oj", "om", "or", "os", "pa", "pi", "pl", "ps",
    "pt", "qu", "rm", "rn", "ro", "ru", "rw", "sa", "sc", "sd", "se", "sg", "si",
    "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw", "ta",
    "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz", "ve", "vi", "vo", "wa", "wo", "xh", "yi", "yo", "za",
    "zh", "zu",
)

# The education facet reports groups, but the filter takes the ISCED and
# EQF levels behind each group (mapping from the portal's frontend). A
# group name sent as a filter value matches nothing.
_EDUCATION_FILTER_CODES = {
    "basic": ("isced_0", "isced_1", "isced_2", "eqf_1", "eqf_2", "eqf_3"),
    "medium": ("isced_3", "isced_4", "eqf_4"),
    "tertiary": ("isced_5", "eqf_5"),
    "bachelor": ("isced_6", "eqf_6"),
    "master": ("isced_7", "eqf_7"),
    "doctoral": ("isced_8", "eqf_8"),
    "not_classified": ("isced_9",),
}

# ISCO-08 groups as ESCO concept URIs. The filter matches a group and
# everything below it (C2 > C25 > C251 > C2512), and a vacancy may carry
# several occupations.
_ISCO_URI_PREFIX = "http://data.europa.eu/esco/isco/C"
_ISCO_UNIT_DIGITS = 4


@dataclass(frozen=True)
class _Child:
    values: tuple[Any, ...]
    # Count announced by the parent's facet; None for occupations.
    count: int | None


@dataclass(frozen=True)
class _Split:
    dimension: _Dimension
    children: tuple[_Child, ...]

    def union_values(self) -> list[Any]:
        return [value for child in self.children for value in child.values]

# Many EURES rows ship with a placeholder employer — confirmed
# 86% of FR rows ("non renseigné") and 60% of ES rows ("") in a
# May 2026 dump. These are real jobs (titles, descriptions and
# locations are all meaningful) but the employer is hidden by the
# source NES (France Travail, SEPE, …) for privacy reasons and is
# only revealed once a candidate applies via the official portal.
#
# Earlier versions dropped these rows entirely — costing the 1.7 M
# FR+ES catalog the user asked us to keep. We now pass the source
# value through verbatim (including the localized placeholder
# string or empty value): the locale of the placeholder is itself
# useful signal about the source NES, and downstream consumers
# can decide how to render it without us hard-coding a canonical
# English marker on their behalf.


def _empty_search_body(rpp: int = PAGE_SIZE, page: int = 1) -> dict[str, Any]:
    """Skeleton search body. Extra keys override the empty defaults."""
    return {
        "resultsPerPage": rpp,
        "page": page,
        "sortSearch": "MOST_RECENT",
        "keywords": [],
        "publicationPeriod": None,
        "occupationUris": [],
        "skillUris": [],
        "requiredExperienceCodes": [],
        "positionScheduleCodes": [],
        "sectorCodes": [],
        "educationAndQualificationLevelCodes": [],
        "positionOfferingCodes": [],
        "locationCodes": [],
        "euresFlagCodes": [],
        "otherBenefitsCodes": [],
        "requiredLanguages": [],
        "minNumberPost": None,
        "sessionId": "ats-scrapers",
        "requestLanguage": "en",
    }


# The ``keywords`` body field expects a list of objects, not plain strings
# (a list of strings is rejected with 400 "invalid-json" — measured live):
#     "keywords": [{"keyword": "Praktikum", "specificSearchCode": "..."}]
# ``specificSearchCode`` values observed in the official frontend bundle:
# EVERYWHERE (title+description+employer), TITLE, DESCRIPTION, EMPLOYER.
# Measured in DE for "Praktikum": EVERYWHERE 35,486 · TITLE 299 ·
# DESCRIPTION 3,706 · EMPLOYER 1. Multiple entries are ANDed by the API
# (Praktikum+internship+trainee together → 79 rows), so pass a single
# keyword per query unless an intersection is intended.
KEYWORD_SEARCH_CODE = "EVERYWHERE"


def _keyword_entries(keywords: list[str]) -> list[dict[str, Any]]:
    return [
        {"keyword": keyword, "specificSearchCode": KEYWORD_SEARCH_CODE}
        for keyword in keywords
    ]


@ScraperRegistry.register(ATSType.EURES)
class EuresScraper(BaseScraper):
    """EURES (EU public employment services) jobs API. Single-source —
    ``company_slug`` is ignored."""

    ats = ATSType.EURES

    default_headers: ClassVar[dict[str, str] | None] = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        keywords: list[str] | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        # Optional keyword filter, opt-in. ``None`` (default) sends an empty
        # ``keywords`` list — full catalogue, zero behaviour change. When
        # set, each keyword becomes a ``{"keyword": ..., "specificSearchCode":
        # "EVERYWHERE"}`` entry (see ``_keyword_entries`` for the measured
        # shape and the AND semantics of multiple entries).
        self.keywords = list(keywords) if keywords else None

    def get_description(self, job: Job) -> str | None:
        if job.description:
            cleaned = _clean_description_text(job.description)
            if cleaned:
                return cleaned
        if not job.ats_id:
            return None
        return self._run_sync(self._aget_description(job))

    async def _aget_description(self, job: Job) -> str | None:
        try:
            async with self.make_fetcher(follow_redirects=False) as fetcher:
                payload = await fetcher.get_json(
                    DETAIL_API_URL_FMT.format(jv_id=job.ats_id)
                )
        except (ScraperError, ValueError):
            return None
        return _extract_detail_description(payload)

    async def fetch_stream(self) -> AsyncGenerator[Job, None]:
        """Stream jobs as they're parsed.

        Memory profile: ~200 MB regardless of corpus size — only the
        ``seen`` ID set + a bounded in-flight queue stays resident.
        Prefer this over the in-memory :meth:`fetch` / :meth:`afetch`
        from cron contexts that write straight to disk — at ~2.7 M jobs
        the accumulated list is ~10 GB RSS, more than a small server has.
        The producer-side fan-out and dedup logic is shared with the
        legacy :meth:`fetch` via :meth:`afetch`; we just plug a
        queue-pushing ``on_job`` callback into it and yield from the
        queue on the consumer side.

        Termination uses an ``asyncio.Event`` rather than a queue
        sentinel: the consumer polls ``queue.get`` with a 500 ms
        timeout and checks ``producer_done`` between polls. This
        avoids the deadlock that a bounded-queue sentinel-put would
        introduce if the consumer ever stops draining (cubic PR #69
        P1) and means producer cleanup is always non-blocking.

        Usage when the full output would exceed RAM:

        .. code-block:: python

            scraper = EuresScraper("eures", timeout=30)
            async for job in scraper.fetch_stream():
                writer.writerow(_job_to_row(job))
        """
        queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=2000)
        producer_done = asyncio.Event()

        async def on_job(job: Job) -> None:
            await queue.put(job)

        async def producer() -> None:
            try:
                await self.afetch(on_job=on_job)
            finally:
                producer_done.set()

        task = asyncio.create_task(producer())
        try:
            while True:
                if producer_done.is_set() and queue.empty():
                    # Propagate any producer exception.
                    await task
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue  # re-check producer_done on next iteration
                yield item
        except BaseException:
            task.cancel()
            raise

    async def afetch(
        self,
        *,
        on_job: Callable[[Job], Awaitable[None]] | None = None,
    ) -> list[Job]:
        """Crawl the whole portal, split by ``_exhaust_query``, with dedup.

        Two modes:

        - ``on_job is None`` (default): accumulate every deduped job
          into a list and return it. Used by :meth:`fetch` for small-
          corpus / test paths.

        - ``on_job`` set to an async callback: dispatch each deduped
          job to the callback instead of accumulating. Used by
          :meth:`fetch_stream` so the queue consumer can write jobs
          to disk as they land; the in-memory footprint drops to just
          the ``seen`` ID set (~100 MB at full corpus). Returns an
          empty list in this mode.
        """
        seen: set[str] = set()
        all_jobs: list[Job] = []
        lock = asyncio.Lock()

        # ``follow_redirects=False`` matters: the CDN's transient 307
        # "Network Error" responses must surface to ``_search`` so it
        # can retry them instead of httpx chasing the redirect into an
        # HTML error page.
        async with self.make_fetcher(follow_redirects=False) as fetcher:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def absorb(items: list[dict[str, Any]]) -> None:
                # Dedup under the lock, then dispatch to the sink outside
                # the lock so a slow ``on_job`` callback can't serialise
                # every absorbing task on the lock.
                new_jobs: list[Job] = []
                async with lock:
                    for it in items:
                        job = self._parse(it)
                        if job is None or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        new_jobs.append(job)
                if on_job is not None:
                    for job in new_jobs:
                        await on_job(job)
                else:
                    all_jobs.extend(new_jobs)

            await self._exhaust_query(
                fetcher, sem, base={}, absorb=absorb,
                slices=asyncio.Semaphore(MAX_ACTIVE_SLICES),
            )
        return all_jobs

    async def _exhaust_query(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
        slices: asyncio.Semaphore | None = None,
    ) -> None:
        """Pull every job matching ``base``, splitting it while it is
        above the pagination cap. ``slices`` bounds how many slices read
        their pages at once."""
        if slices is None:
            slices = asyncio.Semaphore(MAX_ACTIVE_SLICES)

        async def read_capped() -> None:
            async with slices:
                await self._fan_out_pages(
                    fetcher, sem, base=base, total=PAGINATION_CAP, absorb=absorb,
                )

        async def read_whole() -> dict[str, Any] | None:
            """Read a slice that fit under the cap when planned, starting
            from a fresh page 1: while it waited for its turn, modified
            vacancies moved to the top and the slice may have grown.
            Returns that page 1 if the slice no longer fits."""
            async with slices:
                fresh = await self._search(fetcher, sem, base=base, page=1)
                total = int(fresh.get("numberRecords") or 0)
                items = fresh.get("jvs") or []
                await absorb(items)
                if total > PAGINATION_CAP:
                    return fresh
                first_page = _slim(items)
                del fresh, items
                await self._fan_out_pages(
                    fetcher, sem, base=base, total=total, absorb=absorb,
                    first_page=first_page,
                )
                return None

        payload = await self._search(fetcher, sem, base=base, page=1)
        total = int(payload.get("numberRecords") or 0)
        if total == 0:
            return
        await absorb(payload.get("jvs") or [])
        if total <= PAGE_SIZE:
            return
        if total <= PAGINATION_CAP:
            # Full rows are not kept while the slice waits for its turn.
            del payload
            grown = await read_whole()
            if grown is None:
                return
            payload, total = grown, int(grown.get("numberRecords") or 0)

        split = _plan_split(payload.get("facets") or {}, base, total)
        # Nor while its children are read.
        del payload
        if split is None:
            log.warning(
                "EURES slice %s holds %d vacancies and cannot be split "
                "further; %d beyond the first %d are not reachable",
                _describe(base), total, total - PAGINATION_CAP, PAGINATION_CAP,
            )
            await read_capped()
            return

        dimension = split.dimension
        # Vacancies without an occupation cannot be isolated. When an
        # occupation split may leave some out, reading the slice itself
        # keeps those in its first pages.
        read_slice = False
        # Values of one filter are ORed, so this counts the union of the
        # children. It also catches facet values the filter does not accept.
        try:
            union = await self._search(
                fetcher, sem,
                base={**base, dimension.field: split.union_values()},
                page=1, size=1,
            )
        except ScraperError as exc:
            log.warning(
                "EURES could not check the split of %s by %s: %s",
                _describe(base), dimension.name, exc,
            )
            read_slice = dimension is _OCCUPATION
        else:
            covered = int(union.get("numberRecords") or 0)
            # Its facets are not kept while the children are read.
            del union
            if covered < total:
                log.warning(
                    "EURES split of %s by %s covers %d of %d vacancies; "
                    "%d fall outside it",
                    _describe(base), dimension.name, covered, total,
                    total - covered,
                )
                read_slice = dimension is _OCCUPATION

        async def child_slice(child: _Child) -> None:
            child_base = {**base, dimension.field: list(child.values)}
            try:
                await self._exhaust_query(
                    fetcher, sem, base=child_base, absorb=absorb, slices=slices,
                )
            except ScraperError as exc:
                # Failures inside the child's own children are logged
                # there, so this is the child's first request.
                announced = "?" if child.count is None else child.count
                raise ScraperError(
                    f"{_describe(child_base)} ({announced} announced): {exc}"
                ) from exc

        tasks = [child_slice(child) for child in split.children]
        if read_slice:
            tasks.append(read_capped())
        await _gather_tolerant(tasks, label=dimension.name)

    async def _fan_out_pages(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        total: int,
        absorb: Callable[[list[dict[str, Any]]], Awaitable[None]],
        first_page: list[dict[str, Any]] | None = None,
    ) -> None:
        """Read pages 2.. of a slice whose page 1 the caller absorbed.

        With ``first_page``, a slice that comes back short is checked for
        vacancies repeated across pages, and the runs of tied vacancies
        around them are read again (``_rereads``).
        """
        page_count = min((total + PAGE_SIZE - 1) // PAGE_SIZE, PAGE_LIMIT)
        page_label = f"page of {_describe(base)}"
        pages: dict[int, list[dict[str, Any]]] = {}
        seen: set[str] = set()

        def keep(page: int | None, items: list[dict[str, Any]]) -> None:
            if first_page is None:
                return
            slim = _slim(items)
            if page is not None:
                pages[page] = slim
            seen.update(item["id"] for item in slim)

        async def one(page: int) -> None:
            payload = await self._search(fetcher, sem, base=base, page=page)
            items = payload.get("jvs") or []
            keep(page, items)
            await absorb(items)

        if first_page is not None:
            keep(1, first_page)
        await _gather_tolerant(
            (one(p) for p in range(2, page_count + 1)), label=page_label,
        )
        if first_page is None:
            return

        windows_read: set[_Reread] = set()
        refresh: set[int] = set()

        async def window(reread: _Reread) -> None:
            payload = await self._search(
                fetcher, sem, base=base, page=reread.page, size=reread.size,
            )
            items = payload.get("jvs") or []
            keep(None, items)
            await absorb(items)
            if not reread.holds_run(items):
                refresh.update(_pages_around(reread.first, reread.last))

        for _ in range(_REREAD_ROUNDS):
            if len(seen) >= total:
                return
            # A window that held its run is not read twice. Pages are,
            # since a run that moved shows up again in the fresh pages.
            todo = [r for r in _rereads(pages) if r not in windows_read]
            windows_read.update(r for r in todo if r.size)
            regular = {r.page for r in todo if not r.size} | refresh
            refresh = set()
            regular &= set(range(1, page_count + 1))
            if not todo and not regular:
                break
            await _gather_tolerant(
                [
                    *(window(r) for r in todo if r.size),
                    *(one(page) for page in sorted(regular)),
                ],
                label=page_label,
            )
        if len(seen) < total:
            log.warning(
                "EURES slice %s returned %d of %d vacancies",
                _describe(base), len(seen), total,
            )

    async def _search(
        self,
        fetcher: Fetcher,
        sem: asyncio.Semaphore,
        *,
        base: dict[str, Any],
        page: int,
        size: int | None = None,
    ) -> dict[str, Any]:
        body = _empty_search_body(size or PAGE_SIZE, page)
        body.update(base)
        # Opt-in keyword filter (see ``__init__``): overrides the empty
        # ``keywords`` default with the API's object shape.
        if self.keywords:
            body["keywords"] = _keyword_entries(self.keywords)
        # Network errors and 429/5xx are retried inside the shared
        # Fetcher. Two provider quirks stay here via ``handled``:
        #
        # - 400 means a page past the cap or a filter value the API does
        #   not know. Neither is ever requested on purpose, so it raises
        #   and the caller logs the lost slice.
        # - 307 with an HTML "Network Error" body is the CDN/load-
        #   balancer in front of EURES timing out; the next attempt
        #   routes through a fresh upstream and almost always succeeds,
        #   so we retry it here like the Fetcher retries 429/5xx.
        #   Observed 2026-05-11: 5 711 page failures were
        #   307-with-error-page, costing ~285 k rows of the EURES
        #   corpus when the previous code treated 307 as terminal.
        #   (Retry budget/backoff read the fetch-module defaults at
        #   call time, same as the Fetcher itself.)
        retries = fetch_mod.DEFAULT_RETRIES
        for attempt in range(1, retries + 1):
            async with sem:
                r = await fetcher.request(
                    "POST", API_URL, json=body, handled={307, 400},
                )
            if r.status_code == 400:
                raise ScraperError(
                    f"EURES rejected the query {_describe(base)} page={page}"
                )
            if r.status_code == 307:
                if attempt == retries:
                    raise ScraperError(
                        f"EURES returned 307 after "
                        f"{retries} retries for {base} page={page}"
                    )
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(
                        fetch_mod.DEFAULT_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                        fetch_mod.DEFAULT_MAX_RETRY_DELAY,
                    )
                )
                await asyncio.sleep(delay)
                continue
            try:
                return r.json()
            except ValueError as exc:
                raise ScraperError(
                    f"EURES returned non-JSON for {base}: {exc}"
                ) from exc
        raise ScraperError(
            f"EURES exhausted retries for {base} page={page}"
        )

    def _parse(self, item: dict[str, Any]) -> Job | None:
        jv_id = item.get("id")
        title = (item.get("title") or "").strip()
        if not jv_id or not title:
            return None

        # Employer — sometimes nested in ``employerName``, sometimes a
        # flat string. The source NES often anonymizes the employer
        # for privacy reasons (FR uses "non renseigné" at ~86%,
        # ES uses an empty string at ~60%). Pass the source value
        # through verbatim — see the module-level comment for the
        # rationale around keeping the localized placeholder text
        # instead of canonicalizing it.
        employer = (
            item.get("employerName")
            or (item.get("employer") or {}).get("name")
            or ""
        ).strip()

        location = _flatten_location(item.get("locationMap") or {})
        posted_at = _epoch_ms_to_dt(item.get("creationDate"))

        # EURES ships a freeform-ish ``positionOfferingCode``
        # ("directhire", "temporary", "contract", "apprenticeship",
        # "seasonal", "oncall", "selfemployed", …) — map to the
        # canonical employment-type enum and surface the original
        # code as ``commitment`` for display.
        offering = item.get("positionOfferingCode")
        commitment: str | None = None
        employment_type: str | None = None
        if isinstance(offering, str) and offering.strip():
            commitment = offering.strip()
            norm = commitment.lower()
            employment_type = _OFFERING_CODE_TO_EMPLOYMENT_TYPE.get(norm)
            if not employment_type:
                for needle, mapped in _OFFERING_CODE_TO_EMPLOYMENT_TYPE.items():
                    if needle in norm:
                        employment_type = mapped
                        break

        # The schedule (full-time / part-time) is the fallback when the
        # contract type is missing or unspecific. Search rows carry a
        # ``positionScheduleCodes`` list.
        schedules = item.get("positionScheduleCodes")
        if not isinstance(schedules, list):
            schedules = [item.get("positionScheduleCode")]
        for schedule in schedules if not employment_type else ():
            sched_norm = schedule.strip().lower() if isinstance(schedule, str) else ""
            if sched_norm in ("fulltime", "full-time", "full_time"):
                employment_type = "FULL_TIME"
            elif sched_norm in ("parttime", "part-time", "part_time"):
                employment_type = "PART_TIME"
            if employment_type:
                break

        raw: dict[str, Any] = {}
        for k in ("euresFlag", "numberOfPosts", "lastModificationDate",
                  "positionOfferingCode", "positionScheduleCodes",
                  "positionScheduleCode"):
            v = item.get(k)
            if v not in (None, "", []):
                raw[k] = v

        return Job(
            url=DETAIL_URL_FMT.format(jv_id=jv_id),
            title=title,
            company=employer,
            ats_type=ATSType.EURES,
            ats_id=str(jv_id),
            location=location,
            employment_type=employment_type,
            commitment=commitment,
            description=_extract_description(item),
            posted_at=posted_at,
            fetched_at=datetime.now(UTC),
            raw=raw or None,
        )


# EURES ``positionOfferingCode`` is a stable enum across PES feeds.
_OFFERING_CODE_TO_EMPLOYMENT_TYPE = {
    "directhire": "FULL_TIME",
    "permanent": "FULL_TIME",
    "regular": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "contracttohire": "CONTRACT",
    "selfemployed": "CONTRACT",
    "freelance": "CONTRACT",
    "temporary": "TEMPORARY",
    "temporarytohire": "TEMPORARY",
    "seasonal": "TEMPORARY",
    "oncall": "TEMPORARY",
    "casual": "TEMPORARY",
    "apprenticeship": "INTERN",
    "internship": "INTERN",
    "trainee": "INTERN",
    "traineeship": "INTERN",
}


def _epoch_ms_to_dt(value: object) -> datetime | None:
    """EURES dates are unix-epoch milliseconds. Convert to UTC datetime
    or return None on missing/garbage."""
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000)


def _flatten_location(loc_map: dict[str, Any]) -> str | None:
    """``locationMap`` is ``{"DE": ["DE12", "DE34"], ...}``. Render the
    first country's code(s) as a short string."""
    if not loc_map:
        return None
    country = next(iter(loc_map))
    regions = [r for r in (loc_map[country] or []) if isinstance(r, str)]
    if regions:
        return f"{country} ({', '.join(regions[:3])})"
    return country


def _extract_description(item: dict[str, Any]) -> str | None:
    if body := _description_field(item):
        return body
    translations = item.get("translations")
    if isinstance(translations, dict):
        for translation in translations.values():
            if body := _description_field(translation):
                return body
    return None


def _description_field(record: object) -> str | None:
    value = record.get("description") if isinstance(record, dict) else None
    return _clean_description_text(value) if isinstance(value, str) else None


def _extract_detail_description(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    profiles = payload.get("jvProfiles")
    if isinstance(profiles, dict):
        preferred = payload.get("preferredLanguage")
        for language in sorted(profiles, key=lambda lang: lang != preferred):
            if body := _description_field(profiles[language]):
                return body
    return _description_field(payload.get("translation")) or _extract_description(payload)


def _clean_description_text(value: str) -> str | None:
    text = re.sub(r"<br\s*/?>", "\n", decode_encoded_tags(value), flags=re.IGNORECASE)
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?\s*/?>", " ", text)
    text = re.sub(r"[\u200b\ufeff]", "", html.unescape(text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()[:25_000]
    return text if re.search(r"[^\s\u200c\u200d]", text) else None


async def _gather_tolerant(
    coros: Any,
    *,
    label: str,
) -> None:
    """Run every coroutine concurrently, log + swallow failures instead
    of cancelling siblings.

    The default ``asyncio.gather`` re-raises the first exception, which
    cancels every other pending task — one transient network blip in a
    deep recursion (300+ sub-queries per country) used to abort the
    whole scrape and leave the CSV at ~12 k of the ~1 M corpus
    (observed on the 2026-05-11 cron). With this helper, a failed
    sibling logs a warning and the rest of the tree keeps writing.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            log.warning("EURES %s subtask failed: %s", label, r)


def _slim(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ids and timestamps only, all ``_rereads`` needs of a page."""
    return [
        {"id": str(item.get("id")),
         "lastModificationDate": item.get("lastModificationDate")}
        for item in items
    ]


@dataclass(frozen=True)
class _Reread:
    """A regular page (``size`` None) or a custom-size window that must
    return the whole run of ``rows`` vacancies stamped ``stamp``, which
    held positions ``first``..``last`` in the regular pages."""

    page: int
    size: int | None = None
    stamp: Any = None
    rows: int = 0
    first: int = 0
    last: int = 0

    def holds_run(self, items: list[dict[str, Any]]) -> bool:
        """Whether a window came back with its run whole. Vacancies that
        arrived or left since the regular pages shift the run, so an edge
        the window left room at must lie outside it."""
        stamps = [item.get("lastModificationDate") for item in items]
        if stamps.count(self.stamp) < self.rows:
            return False
        top = (self.page - 1) * (self.size or PAGE_SIZE)
        if top < self.first and stamps and stamps[0] == self.stamp:
            return False
        bottom = top + (self.size or PAGE_SIZE) - 1
        return not (
            bottom > self.last
            and len(stamps) == self.size
            and stamps[-1] == self.stamp
        )


def _pages_around(first: int, last: int) -> range:
    """The regular pages covering positions ``first``..``last`` and their
    neighbours."""
    low, high = max(0, first - 1), last + 1
    return range(low // PAGE_SIZE + 1, high // PAGE_SIZE + 2)


def _rereads(pages: dict[int, list[dict[str, Any]]]) -> list[_Reread]:
    """Requests for each run of vacancies sharing a ``lastModificationDate``
    around a vacancy seen on two pages.

    Results are ordered by that date, and the order inside a run changes
    over time, so pages read moments apart can repeat one vacancy of a run
    and skip another. A run that fits in one page is read again in a
    single window of a custom size, with room around it where possible;
    a longer run gets its regular pages read again. A repeat with other
    timestamps between its two places is a vacancy that arrived or left
    meanwhile, which skips nothing.
    """
    stamps: dict[int, Any] = {}
    found_at: dict[str, list[int]] = {}
    for page, items in pages.items():
        for index, item in enumerate(items):
            position = (page - 1) * PAGE_SIZE + index
            stamps[position] = item.get("lastModificationDate")
            found_at.setdefault(str(item.get("id")), []).append(position)
    runs: set[tuple[Any, int, int]] = set()
    for positions in found_at.values():
        if len(positions) < 2:
            continue
        stamp = stamps[positions[0]]
        start, end = min(positions), max(positions)
        if any(stamps.get(p, stamp) != stamp for p in range(start, end + 1)):
            continue
        while start - 1 in stamps and stamps[start - 1] == stamp:
            start -= 1
        while end + 1 in stamps and stamps[end + 1] == stamp:
            end += 1
        runs.add((stamp, start, end))
    requests: set[_Reread] = set()
    for stamp, start, end in runs:
        window = _window(start, end)
        if window is None:
            requests.update(_Reread(page) for page in _pages_around(start, end))
        else:
            page, size = window
            requests.add(_Reread(page, size, stamp, end - start + 1, start, end))
    return sorted(requests, key=lambda r: (r.page, r.size or 0))


def _window(start: int, end: int) -> tuple[int, int] | None:
    """``(page, size)`` of the window holding positions ``start``..``end``
    with the most room on its tighter side."""
    best: tuple[int, int, int] | None = None
    for size in range(end - start + 1, PAGE_SIZE + 1):
        index = start // size
        if index != end // size or (index + 1) * size > PAGINATION_CAP:
            continue
        room = min(start - index * size if start else size,
                   (index + 1) * size - 1 - end)
        if best is None or room > best[0]:
            best = (room, index + 1, size)
    return None if best is None else best[1:]


def _plan_split(
    facets: dict[str, Any],
    base: dict[str, Any],
    total: int,
) -> _Split | None:
    """Pick how to split a slice of ``total`` vacancies above the cap.

    Among the facets not yet filtered on, prefer one whose counts reach
    the parent total, then the fewest vacancies left above the cap plus
    repeated across children. Falls back to the next ISCO level.
    """
    candidates: list[tuple[tuple[int, ...], _Split]] = []
    for priority, dimension in enumerate((_LOCATION, *_FACET_DIMENSIONS)):
        if dimension is _LOCATION:
            counted = _location_children(facets, base.get("locationCodes"))
        elif base.get(dimension.field):
            continue
        else:
            counted = _counted(_facet_entries(facets, dimension.facet))
        children = [
            _Child(_filter_values(dimension, code), count)
            for code, count in counted
        ]
        counts = [count for _, count in counted]
        unlisted = _unlisted_languages(facets) if dimension is _LANGUAGE else None
        # An empty filter would match the whole parent again.
        if unlisted is not None and unlisted.values:
            # The vacancies no listed language covers need an unlisted one.
            children.append(unlisted)
            counts.append(max(0, total - sum(counts)))
        if len(counts) < 2 or max(counts) >= total:
            continue
        volume = sum(counts)
        overflow = sum(max(0, count - PAGINATION_CAP) for count in counts)
        duplicates = max(0, volume - total)
        key = (int(volume < total), overflow + duplicates, len(counts), priority)
        candidates.append((key, _Split(dimension, tuple(children))))
    if candidates:
        return min(candidates, key=lambda c: c[0])[1]

    uris = _occupation_children(base.get("occupationUris"))
    if not uris:
        return None
    return _Split(_OCCUPATION, tuple(_Child((uri,), None) for uri in uris))


def _filter_values(dimension: _Dimension, code: str) -> tuple[Any, ...]:
    if dimension is _EDUCATION:
        return _EDUCATION_FILTER_CODES.get(code, (code,))
    if dimension is _LANGUAGE:
        return ({"isoCode": code, "level": _LANGUAGE_ANY_LEVEL},)
    return (code,)


def _unlisted_languages(facets: dict[str, Any]) -> _Child:
    listed = {e.get("code") for e in _facet_entries(facets, _LANGUAGE.facet)}
    return _Child(
        tuple(
            {"isoCode": code, "level": _LANGUAGE_ANY_LEVEL}
            for code in _ISO_639_1
            if code not in listed
        ),
        None,
    )


def _facet_entries(facets: dict[str, Any], name: str | None) -> list[dict[str, Any]]:
    facet = (facets or {}).get(name or "") or {}
    entries = facet.get("facetEntriesList") if isinstance(facet, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def _counted(entries: list[dict[str, Any]]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for entry in entries:
        code = entry.get("code")
        count = int(entry.get("count") or 0)
        if isinstance(code, str) and code and count > 0:
            out.append((code, count))
    return out


def _location_children(
    facets: dict[str, Any],
    selected: list[str] | None,
) -> list[tuple[str, int]]:
    """Countries for an unlocated slice, the regions of a country, and
    nothing below a region. ``NS`` and ``<country>-NS`` hold vacancies
    without a country or region."""
    entries = _facet_entries(facets, _LOCATION.facet)
    if not selected:
        return _counted(entries)
    if len(selected) != 1:
        return []
    target = selected[0].lower()
    for entry in entries:
        if str(entry.get("code") or "").lower() == target:
            children = entry.get("childrenList") or []
            return _counted([c for c in children if isinstance(c, dict)])
    return []


def _occupation_children(selected: list[str] | None) -> list[str]:
    if not selected:
        return [f"{_ISCO_URI_PREFIX}{d}" for d in range(10)]
    if len(selected) != 1 or not selected[0].startswith(_ISCO_URI_PREFIX):
        return []
    code = selected[0].removeprefix(_ISCO_URI_PREFIX)
    if len(code) >= _ISCO_UNIT_DIGITS:
        return []
    return [f"{_ISCO_URI_PREFIX}{code}{d}" for d in range(10)]


def _describe(base: dict[str, Any]) -> str:
    parts = []
    for key, value in base.items():
        if not isinstance(value, list) or not value:
            continue
        labels = [_label(v) for v in value[:8]]
        if len(value) > len(labels):
            labels.append(f"+{len(value) - len(labels)}")
        parts.append(f"{key}={','.join(labels)}")
    return " ".join(parts).replace(_ISCO_URI_PREFIX, "isco:") or "(all)"


def _label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("isoCode") or value)
    return str(value)
