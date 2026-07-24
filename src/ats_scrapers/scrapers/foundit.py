"""Foundit (formerly Monster India) — India + SEA jobs aggregator.

Foundit is the rebrand of Monster.com's India / APAC operations.
It runs separate country domains, each fronted by the same backend:

  - ``https://www.foundit.in/``      — India (~300k live postings)
  - ``https://www.foundit.sg/``      — Singapore (~125k)
  - ``https://www.foundit.my/``      — Malaysia (~35k)
  - ``https://www.foundit.id/``      — Indonesia (~17k)
  - ``https://www.foundit.com.ph/``  — Philippines (~37k)

The whole site is fronted by **Akamai Bot Manager** (the ``_abck`` /
``bm_sz`` cookies on the HTML pages give it away) — the SPA HTML
returns 200 for the homepage but blocks deep search URLs with
``Access Denied`` from ``errors.edgesuite.net``. However the JSON
**search API** at ``/middleware/jobsearch`` is anonymous, accepts
plain ``httpx`` (no TLS-fingerprint check), and returns the same
structured rows the SPA renders client-side. We hit the API
directly with a Chrome-ish ``User-Agent`` + ``Referer`` and skip the
SPA entirely.

API reverse-engineered live on 2026-05-12. Query params:

    sort=1            most-recent first (0 = relevance)
    limit=100         page size; >100 silently truncates extras
    query=            free-text search; empty == all jobs
    searchId=         session-id cookie; empty works for anon
    queryDerived=true required for the no-query "all jobs" view
    country=india     country token (matches the domain channel)
    start=N           offset; capped around 9500 (see below)

Each entry in ``jobSearchResponse.data`` carries the job id, title,
company, location, salary range (already structured + ISO 4217
currency), skills, experience window, qualifications, posted-at
epoch, and the ``jdUrl`` slug. Description prose lives on the
detail page (Next.js ``__NEXT_DATA__``) — we don't fetch per-job by
default because the catalogue is large; downstream LLM enrichment
fills it in.

**Deep-pagination cap**: the API silently clamps ``start`` to ~9500.
Past that, the same rows repeat with wrap-around cursors (verified
live: ``start=10000`` returns the same payload as ``start=9500``).
So the unrestricted ``"all jobs"`` walk tops out at ~10 000 / 300 000
on India. The same cap applies *per query bucket* — even with
``query=engineer`` (62k hits) we can only read the first 9500 rows.

**Keyword bucketing** (``bucket_strategy="keyword"``) opt-in mode
works around the cap by iterating ~80 broad seed terms (engineer,
manager, sales, …). Each seed yields its own top-9500 window;
the union — deduped by ``ats_id`` — recovers a large fraction of
the ~522k catalogue. Coverage isn't 100 % (a job nobody's seeds
hit gets missed), but live runs on India typically lift the take
from ~10k to 200k+. The default ``"none"`` mode preserves the
original single-call no-query behaviour for backwards-compat.

Single-source scraper, multi-region: ``company_slug`` picks the
country. Pass one of ``"in"`` (default), ``"sg"``, ``"my"``,
``"id"``, ``"ph"``. Aliases (``"india"``, ``"singapore"``, …) are
also accepted and canonicalised to the two-letter slug.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import httpx

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, EmploymentType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

BucketStrategy = Literal["none", "keyword"]

log = logging.getLogger(__name__)


# --- Country / domain map ---------------------------------------------

# slug → (domain, country query token, ISO 3166-1 alpha-2, region label).
# The ``country`` query token is what the API expects (not the slug,
# not the alpha-2). India uses ``india``, Singapore ``singapore``, etc.
_COUNTRY_TABLE: dict[str, tuple[str, str, str, str]] = {
    "in": ("www.foundit.in",     "india",       "IN", "Asia"),
    "sg": ("www.foundit.sg",     "singapore",   "SG", "Asia"),
    "my": ("www.foundit.my",     "malaysia",    "MY", "Asia"),
    "id": ("www.foundit.id",     "indonesia",   "ID", "Asia"),
    "ph": ("www.foundit.com.ph", "philippines", "PH", "Asia"),
}

# Accepted alternate forms — canonicalised to the two-letter slug
# above. Lower-cased before lookup.
_COUNTRY_ALIASES: dict[str, str] = {
    "india": "in",
    "singapore": "sg",
    "malaysia": "my",
    "indonesia": "id",
    "philippines": "ph",
    "ph_ph": "ph",
}


# --- API knobs --------------------------------------------------------

# 100 is the largest size the API honours (>100 returns the same 100
# row payload). 100 × pages == real per-page count.
PER_PAGE = 100

# Past ``start ~= 9500`` the API silently wraps to recently-seen
# rows. Capping at 9500 covers the freshest ~10k jobs per channel.
# India alone has 300k+ so the long tail is left for query-seed
# bucketing in a follow-up.
MAX_USABLE_OFFSET = 9500
DEFAULT_MAX_PAGES = (MAX_USABLE_OFFSET // PER_PAGE) + 1  # 96

# Stop a bucket only after this many *consecutive* pages yield no new
# rows. A single empty page is not enough: a keyword bucket's early
# pages can be fully covered by an earlier seed while later pages still
# hold unique jobs, so we must keep paging past the first overlap.

# Retry knobs. The API returns 200 reliably from residential IPs;
# datacenter IPs can see intermittent 403 / 502 — we back off and
# retry a small number of times then bail with whatever we have.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5

# Chrome-on-Linux user agent. The middleware endpoint accepts any
# UA — even ``curl/7.x`` works — but we mirror the real SPA's
# Chrome UA to blend in if the WAF rules tighten later.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


# --- Keyword bucketing -----------------------------------------------

# Broad seed terms for ``bucket_strategy="keyword"``. The set is
# tuned to span the catalogue: white-collar roles (engineer, manager,
# analyst, ...), blue-collar (driver, technician, electrician, ...),
# seniority modifiers (junior / senior / lead / head), broad
# functions (sales, marketing, finance, ...), and language /
# technology pivots (python, java, sql) — the latter add coverage
# in IT-heavy markets like India where the same job appears under
# multiple non-IT seeds too. The list is intentionally English-
# only: live probing showed the API tokenises Latin-script queries
# against multilingual postings reasonably well (Hindi / Tagalog
# job titles still rank against English seeds because foundit
# canonicalises titles internally). Adding non-Latin seeds would
# yield marginal coverage at the cost of brittler parameter
# encoding through Akamai.
#
# Ordering matters slightly: high-volume seeds run first so we
# capture the most jobs before any rate-limit retry budget is
# consumed. Roughly ~80 seeds × ~9500 max each → ~760k possible
# row reads pre-dedup against the 522k true catalogue.
_DEFAULT_KEYWORD_SEEDS: tuple[str, ...] = (
    # high-volume generic
    "engineer", "manager", "developer", "analyst", "executive",
    "consultant", "senior", "specialist", "associate", "officer",
    "lead", "supervisor", "coordinator", "assistant", "intern",
    "head", "director", "vp", "president", "chief",
    # functions
    "sales", "marketing", "finance", "accounting", "accountant",
    "hr", "operations", "logistics", "supply chain", "procurement",
    "design", "designer", "product", "project", "program",
    "research", "quality", "support", "service", "admin",
    # IT-specific
    "software", "java", "python", "javascript", "data",
    "cloud", "devops", "fullstack", "backend", "frontend",
    "android", "ios", "qa", "sre", "security",
    # blue-collar / trades
    "driver", "technician", "electrician", "mechanic",
    "machinist", "welder", "operator", "foreman",
    # services / healthcare / education
    "teacher", "trainer", "doctor", "nurse", "pharmacist",
    "lawyer", "chef", "retail", "hospitality", "warehouse",
    # role descriptors
    "junior", "graduate", "fresher", "entry level", "trainee",
    "customer", "business", "tech", "media", "communications",
)


def _default_keyword_seeds() -> tuple[str, ...]:
    """Return the default seed list (frozen tuple, safe to share).

    Exposed as a helper so callers / tests can introspect or
    override without poking the private constant directly.
    """
    return _DEFAULT_KEYWORD_SEEDS


# --- Mapping helpers --------------------------------------------------

# Foundit's free-text employment label → our canonical enum. Lower-
# cased and stripped before lookup. Unknown values fall through to
# ``None``; ``raw["employment_types"]`` retains the original list.
_EMPLOYMENT_TYPE_MAP: dict[str, EmploymentType] = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "permanent": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "contractual": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "trainee": "INTERN",
    "temporary": "TEMPORARY",
    "casual": "TEMPORARY",
    "walk in": "FULL_TIME",  # Foundit's walk-in interview convention.
}


@ScraperRegistry.register(ATSType.FOUNDIT)
class FounditScraper(BaseScraper):
    """Foundit (Monster India / APAC) — country-paginated job listings.

    Constructor knobs:
        company_slug: country selector. One of ``"in"`` (default,
            India), ``"sg"``, ``"my"``, ``"id"``, ``"ph"`` — or the
            full English name (``"india"``, ``"singapore"``, …).
        max_pages: stop after this many pages **per query bucket**
            even if more remain. Default walks until the API's
            deep-pagination cap (``MAX_USABLE_OFFSET / PER_PAGE``).
        bucket_strategy: ``"none"`` (default) issues a single
            no-query walk capped at ~9500 rows — fastest, fully
            backwards-compatible. ``"keyword"`` iterates ~80
            broad seed terms (``keyword_seeds``), each up to its
            own 9500-row cap, and dedups by job id. Roughly 20-25x
            the row count on India at ~80x the wall-clock cost.
        keyword_seeds: override the default seed list for
            ``bucket_strategy="keyword"``. Useful for smoke tests
            or for tuning to a local market.
    """

    ats = ATSType.FOUNDIT

    def __init__(
        self,
        company_slug: str = "in",
        *,
        timeout: float = 30.0,
        max_pages: int = DEFAULT_MAX_PAGES,
        bucket_strategy: BucketStrategy = "none",
        keyword_seeds: Sequence[str] | None = None,
    ) -> None:
        normalized = (company_slug or "in").lower().strip()
        normalized = _COUNTRY_ALIASES.get(normalized, normalized)
        if normalized not in _COUNTRY_TABLE:
            raise ScraperError(
                f"Foundit unknown country slug {company_slug!r}. "
                f"Known: {sorted(_COUNTRY_TABLE)} (or aliases "
                f"{sorted(_COUNTRY_ALIASES)})"
            )
        if bucket_strategy not in ("none", "keyword"):
            raise ScraperError(
                f"Foundit unknown bucket_strategy {bucket_strategy!r}. "
                f"Known: 'none', 'keyword'."
            )
        super().__init__(normalized, timeout=timeout)
        self.country_slug = normalized
        domain, token, iso, region = _COUNTRY_TABLE[normalized]
        self._domain = domain
        self._country_token = token
        self._country_iso = iso
        self._region = region
        self.max_pages = max_pages
        self.bucket_strategy: BucketStrategy = bucket_strategy
        if keyword_seeds is None:
            self.keyword_seeds: tuple[str, ...] = _DEFAULT_KEYWORD_SEEDS
        else:
            # Dedup-preserving-order; strip empties so callers can
            # pass a casual list without surprises.
            seen_kw: set[str] = set()
            cleaned: list[str] = []
            for kw in keyword_seeds:
                if not isinstance(kw, str):
                    continue
                k = kw.strip()
                if not k or k.lower() in seen_kw:
                    continue
                seen_kw.add(k.lower())
                cleaned.append(k)
            self.keyword_seeds = tuple(cleaned)
        if bucket_strategy == "keyword" and not self.keyword_seeds:
            raise ScraperError(
                "bucket_strategy='keyword' requires at least one non-empty seed"
            )

    # ----- public entry point -----------------------------------------

    async def afetch(self) -> list[Job]:
        return await asyncio.to_thread(self.fetch)

    def fetch(self) -> list[Job]:
        fetched_at = datetime.now(tz=UTC)
        jobs: list[Job] = []
        seen: set[str] = set()
        with httpx.Client(
            timeout=self.timeout,
            headers={
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"https://{self._domain}/",
                "User-Agent": _USER_AGENT,
            },
            follow_redirects=True,
        ) as client:
            if self.bucket_strategy == "keyword":
                self._fetch_keyword_buckets(
                    client, jobs, seen, fetched_at=fetched_at,
                )
            else:
                # Original single-call no-query walk.
                self._walk_query(
                    client, query="", jobs=jobs, seen=seen,
                    fetched_at=fetched_at,
                )

        log.info(
            "Foundit %s [%s]: fetched %d unique jobs",
            self.country_slug, self.bucket_strategy, len(jobs),
        )
        return jobs

    # ----- bucketing --------------------------------------------------

    def _fetch_keyword_buckets(
        self,
        client: httpx.Client,
        jobs: list[Job],
        seen: set[str],
        *,
        fetched_at: datetime,
    ) -> None:
        """Iterate keyword seeds, walking each as its own paginated
        bucket. Mutates ``jobs`` / ``seen`` in place; dedup is by
        ``ats_id`` so a job hit by N seeds counts once.
        """
        total_seeds = len(self.keyword_seeds)
        for idx, keyword in enumerate(self.keyword_seeds, start=1):
            before = len(jobs)
            self._walk_query(
                client, query=keyword, jobs=jobs, seen=seen,
                fetched_at=fetched_at,
            )
            added = len(jobs) - before
            log.debug(
                "Foundit %s keyword %d/%d %r: +%d new jobs (total %d)",
                self.country_slug, idx, total_seeds, keyword,
                added, len(jobs),
            )

    def _walk_query(
        self,
        client: httpx.Client,
        *,
        query: str,
        jobs: list[Job],
        seen: set[str],
        fetched_at: datetime,
    ) -> None:
        """Paginate one query bucket up to ``max_pages`` / the API's
        ~9500 cap. New rows are appended to ``jobs`` and their ids
        added to ``seen``; existing ids are skipped.
        """
        query_seen: set[str] = set()
        for page_no in range(self.max_pages):
            start = page_no * PER_PAGE
            if start > MAX_USABLE_OFFSET:
                break
            payload = self._fetch_page(client, start, query=query)
            if payload is None:
                break
            rows = list(_iter_job_rows(payload))
            if not rows:
                break
            new_for_query = 0
            for row in rows:
                job = self._parse_row(row, fetched_at=fetched_at)
                if job is None:
                    continue
                if job.ats_id in query_seen:
                    continue
                query_seen.add(job.ats_id)  # type: ignore[arg-type]
                new_for_query += 1
                if job.ats_id in seen:
                    continue
                seen.add(job.ats_id)  # type: ignore[arg-type]
                jobs.append(job)
            if new_for_query == 0:
                # The API wrapped to a window already seen in this same
                # query. Cross-seed duplicates do not trigger this stop.
                break

    # ----- one page ---------------------------------------------------

    def _fetch_page(
        self, client: httpx.Client, start: int, *, query: str = "",
    ) -> dict[str, Any] | None:
        """GET one search page with retry on 429 / 5xx. Returns the
        decoded ``jobSearchResponse`` dict or ``None`` to break the
        pagination loop on terminal failure.

        ``query`` is the ``query=`` filter — empty for the unrestricted
        walk, non-empty for keyword bucketing.
        """
        url = f"https://{self._domain}/middleware/jobsearch"
        params = {
            "sort": "1",
            "limit": str(PER_PAGE),
            "query": query,
            "searchId": "",
            "queryDerived": "true",
            "country": self._country_token,
            "start": str(start),
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Foundit %s start=%d transport error after %d "
                        "retries: %s",
                        self.country_slug, start, MAX_RETRIES, exc,
                    )
                    return None
                _sleep_backoff(attempt)
                continue

            status = response.status_code
            if status == 200:
                try:
                    body = response.json()
                except ValueError:
                    log.warning(
                        "Foundit %s start=%d: 200 but non-JSON body — stopping",
                        self.country_slug, start,
                    )
                    return None
                if body.get("jobSearchStatus") != 200:
                    log.info(
                        "Foundit %s start=%d: API status=%s (%s) — stopping",
                        self.country_slug, start,
                        body.get("jobSearchStatus"),
                        body.get("jobSearchStatusText"),
                    )
                    return None
                return body.get("jobSearchResponse") or {}
            if status in (429,) or 500 <= status < 600:
                if attempt == MAX_RETRIES:
                    log.warning(
                        "Foundit %s start=%d returned %d after %d "
                        "retries — stopping pagination",
                        self.country_slug, start, status, MAX_RETRIES,
                    )
                    return None
                _sleep_backoff(attempt)
                continue
            # Some other 4xx — surface so an operator notices.
            log.warning(
                "Foundit %s start=%d returned unexpected status %d — stopping",
                self.country_slug, start, status,
            )
            return None

        log.warning(
            "Foundit %s start=%d exhausted retries: %s",
            self.country_slug, start, last_exc,
        )
        return None

    # ----- single-row parser ------------------------------------------

    def _parse_row(
        self,
        row: dict[str, Any],
        *,
        fetched_at: datetime,
    ) -> Job | None:
        """Map one API row → ``Job``. Returns ``None`` when the row
        lacks the bare minimum (id + title + jdUrl).
        """
        ats_id = _str_or_none(row.get("id")) or _str_or_none(row.get("jobId"))
        if not ats_id:
            return None
        title = _clean_text(row.get("title"))
        jd_url = _str_or_none(row.get("jdUrl")) or _str_or_none(
            row.get("seoJdUrl")
        )
        if not title or not jd_url:
            return None

        url = _absolute_url(self._domain, jd_url)

        company = _clean_text(row.get("companyName")) or "Unknown"
        location = _clean_text(row.get("locations")) or None

        # Posted-at: the row carries multiple timestamps; ``createdAt``
        # is the canonical first-publish moment in epoch milliseconds.
        posted_at = _epoch_ms_to_dt(row.get("createdAt"))

        # Salary — Foundit exposes structured min/max + currency.
        # Currency is a 3-letter ISO code (``INR``, ``SGD``, ``MYR``,
        # ``IDR``, ``PHP``) — pass through to Job verbatim.
        currency = _str_or_none(row.get("currencyCode"))
        min_sal = _nested_amount(row.get("minimumSalary"))
        max_sal = _nested_amount(row.get("maximumSalary"))
        salary_summary = _clean_text(row.get("salary")) or None
        # The ``hideSalary`` flag is set on confidential listings; the
        # numeric values are still in the payload but the SPA hides
        # them. Honour the hint so we don't surface salaries the
        # employer chose not to publish.
        if row.get("hideSalary"):
            min_sal = None
            max_sal = None
            salary_summary = None
            currency = None

        # Employment type: a list — pick the first one we can map.
        employment_label = None
        emp_list = row.get("employmentTypes")
        if isinstance(emp_list, list) and emp_list:
            employment_label = _clean_text(emp_list[0]) or None
        employment_type: EmploymentType | None = None
        if employment_label:
            employment_type = _EMPLOYMENT_TYPE_MAP.get(
                employment_label.lower().strip(),
            )

        # Years of experience — ``minimumExperience`` is the typical
        # "minimum years required". ``None`` when missing or zero
        # (entry-level postings often omit it entirely).
        experience: int | None = None
        min_exp = row.get("minimumExperience")
        if isinstance(min_exp, dict):
            yrs = min_exp.get("years")
            if isinstance(yrs, (int, float)) and yrs > 0:
                experience = int(yrs)

        # Raw overflow — keep the ATS-specific extras the canonical
        # schema can't represent. Keep small (<5KB serialized): drop
        # the verbose ``companyProfile`` blob (~5KB on its own).
        raw_overflow: dict[str, object] = {
            "industry": _clean_text(row.get("industry")) or None,
            "experience_label": _clean_text(row.get("exp")) or None,
            "education": list(_iter_strings(row.get("qualifications"))),
            "skills": _split_skills(row.get("skills")),
            "functions": list(_iter_strings(row.get("functions"))),
            "roles": list(_iter_strings(row.get("roles"))),
            "designations": list(_iter_strings(row.get("designations"))),
            "employment_types": list(_iter_strings(row.get("employmentTypes"))),
            "job_types": list(_iter_strings(row.get("jobTypes"))),
            "country_slug": self.country_slug,
            "channel_name": _clean_text(row.get("channelName")) or None,
            "is_urgently_hiring": bool(row.get("isUrgentlyHiring")),
            "kiwi_job_id": _str_or_none(row.get("kiwiJobId")),
        }

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.FOUNDIT,
            ats_id=ats_id,
            location=location,
            country_iso=self._country_iso,
            region=self._region,
            language="en",
            salary_currency=currency,
            salary_period="YEAR" if currency else None,
            salary_summary=salary_summary,
            salary_min=min_sal,
            salary_max=max_sal,
            experience=experience,
            employment_type=employment_type,
            commitment=employment_label,
            posted_at=posted_at,
            fetched_at=fetched_at,
            raw=raw_overflow,
        )


# --- module-level helpers ---------------------------------------------


def _iter_job_rows(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield the real job rows from a ``jobSearchResponse`` payload.

    The API interleaves ad/banner placements (``{"index": N, "type":
    "adsense"}``) with the actual job entries; those lack the canonical
    fields. We filter on the presence of ``id`` / ``jobId`` since
    every real posting carries one.
    """
    data = payload.get("data")
    if not isinstance(data, list):
        return
    for row in data:
        if not isinstance(row, dict):
            continue
        if "id" not in row and "jobId" not in row:
            continue
        yield row


def _absolute_url(domain: str, path_or_url: str) -> str:
    """Foundit's ``jdUrl`` is a site-relative path. Build the absolute
    URL using the *country* domain (so the public dataset's URL
    matches the channel the job lives on, not the .in default).
    """
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return f"https://{domain}{path_or_url}"


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, (int, float)):
        return str(int(value))
    return None


def _clean_text(value: object) -> str | None:
    """Strip + collapse whitespace, return None for empties. We don't
    decode HTML entities here — Foundit's ``title`` / ``companyName``
    are plain text in practice (the HTML-entity ones live in
    ``companyProfile``, which we don't surface to ``Job.description``).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _nested_amount(value: object) -> float | None:
    """Salary fields are ``{"currency": "INR", "absoluteValue": N,
    "absoluteMonthlyValue": M}``. We use the annualised
    ``absoluteValue``. Zero is treated as "not set" — Foundit
    encodes "no salary disclosed" as a 0 there.
    """
    if not isinstance(value, dict):
        return None
    amount = value.get("absoluteValue")
    if not isinstance(amount, (int, float)):
        return None
    if amount <= 0:
        return None
    return float(amount)


def _epoch_ms_to_dt(value: object) -> datetime | None:
    """Foundit timestamps are epoch milliseconds — convert to UTC.
    Returns ``None`` on missing / malformed input rather than crashing
    the row."""
    if not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _iter_strings(value: object) -> Iterable[str]:
    """Yield clean non-empty strings from a list field. Drops dicts /
    nulls silently."""
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, str):
            s = item.strip()
            if s:
                yield s


def _split_skills(value: object) -> list[str]:
    """``skills`` arrives as a comma-separated string (``"Java, Python,
    SQL"``). Split into a tidy list — keeps ``raw["skills"]`` as a
    proper array rather than a free-text blob."""
    if not isinstance(value, str):
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff for transient 5xx / 429 — factored out so
    tests can monkey-patch it to a no-op and not pay wall-clock cost.
    """
    time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
