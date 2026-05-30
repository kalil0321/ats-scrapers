"""Wuzzuf (https://wuzzuf.net) — Egypt + MENA direct-posting job board.

Wuzzuf is Egypt's largest job platform (~5 k active postings) with a
smaller Saudi Arabia presence (``/saudi/...``). Companies post directly
rather than the site aggregating LinkedIn / Indeed feeds, so coverage is
high-signal but limited in volume.

The site is a Nuxt-style SSR React app: there is no public JSON API
exposed for listings, but the server-rendered HTML at
``/search/jobs/?filters[country][0]={Country}&start={N}`` carries every
visible card pre-hydrated. We scrape that HTML directly.

Pagination is offset-style: ``?start=0`` shows the first 15 cards,
``?start=15`` shifts by one page, etc. The catalogue caps at ~340
records across all categories regardless of the reported total
(``5,415 Job Opportunities in Egypt`` in the meta), so the safety cap
of ``MAX_PAGES=200`` is far above what's reachable today; the scraper
stops naturally when a page returns zero job links.

Multi-country handling: pass ``company_slug`` as the country segment
(``"egypt"`` — default — ``"saudi-arabia"``, or ``"all"`` to enumerate
every supported country). The country segment determines the search
URL prefix (``/search/jobs`` for Egypt, ``/saudi/search/jobs`` for
Saudi) and the ``filters[country][0]`` query value. Output rows carry
``country_iso`` (``EG`` / ``SA``) so downstream consumers can filter
without re-deriving from ``location`` text.

The site honours ``language=en`` cookie / ``Accept-Language`` header to
keep titles in English; without it Saudi search 302-redirects to the
Arabic ``/ar/saudi/...`` mirror.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

BASE_URL = "https://wuzzuf.net"
PER_PAGE = 15  # Wuzzuf renders 15 cards per ``start`` increment.
MAX_PAGES = 200  # Safety cap. Real catalogue caps at ~23 pages today.
MAX_CONCURRENCY = 3  # Be polite to the SSR — each page is ~600 kB HTML.
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5

# Country segment → (URL prefix, ``filters[country][0]`` value, ISO 3166-1).
# Probed 2026-05-12: the ``/saudi`` prefix exists; ``/uae`` 404s. Add
# more rows here when Wuzzuf launches additional country mirrors.
COUNTRY_SEGMENTS: dict[str, tuple[str, str, str]] = {
    "egypt": ("", "Egypt", "EG"),
    "saudi-arabia": ("/saudi", "Saudi Arabia", "SA"),
}

# Currency per country — Wuzzuf rarely surfaces structured salary, but
# when ``salary_summary`` is present we tag it with the local currency
# so downstream parse_salary_range has a working denomination.
_CURRENCY_BY_ISO: dict[str, str] = {
    "EG": "EGP",
    "SA": "SAR",
}

# Card-level regexes. Each card is one ``<div class="css-ghe2tq ..."> ... </div>``
# block; we split on the link to the posting then peel fields off the
# surrounding markup. The Emotion class names (``css-xxxxx``) are
# obfuscated but stable across renders — we still anchor on structural
# tags (``<h2>``, ``<a href=>``, ``<span>``) where possible so the
# parser survives a CSS rename.
_JOB_LINK_RE = re.compile(
    r'<a[^>]*\bhref="(?P<href>(?:/[a-z-]+)?/jobs/p/(?P<id>[A-Za-z0-9]+)-[^"]+)"'
    r'[^>]*>(?P<title>[^<]+)</a>',
)
_COMPANY_RE = re.compile(
    r'href="https?://wuzzuf\.net/jobs/careers/[^"]+"[^>]*class="css-ipsyv7"[^>]*>(?P<company>[^<]+?)\s*-\s*</a>',
)
# Location lives in the ``css-16x61xq`` span and ships as
# ``City, <!-- -->Country `` or ``District, City, Country``. Strip the
# HTML comment + trailing whitespace.
_LOCATION_RE = re.compile(
    r'<span[^>]*class="css-16x61xq"[^>]*>(?P<loc>.*?)</span>',
    re.DOTALL,
)
# Posted-at lives in ``css-eg55jf`` — ``"X minutes ago"`` / ``"1 hour ago"``
# / ``"2 days ago"`` etc.
_POSTED_RE = re.compile(
    r'<div[^>]*class="css-eg55jf"[^>]*>(?P<posted>[^<]+)</div>',
)
# Employment-type / modality badges. ``Full Time`` / ``Part Time`` /
# ``On-site`` / ``Remote`` / etc. render as styled pills:
# ``<a href="/a/{slug}-Jobs-in-{Country}"><span class="css-...">label</span></a>``.
# The ``<span>`` wrapping is what visually distinguishes a badge from
# the level (``<a class="css-o171kl" ...>Manager</a>``) and category
# anchors — anchor a regex on the inner ``<span>`` so we only pick
# real pills, not every ``/a/`` link in the card body.
_BADGE_RE = re.compile(
    # ``/a/`` for Egypt, ``/saudi/a/`` for Saudi, etc. — match an optional
    # country segment before the ``/a/`` so we work across mirrors.
    r'<a[^>]*\bhref="(?:/[a-z-]+)?/a/(?P<slug>[A-Za-z][A-Za-z0-9-]*)-Jobs-in-[^"]+"[^>]*>'
    # Zero or more inline ``<style>`` blocks (Emotion injects these on
    # every render) interleaved with whitespace. ``\s*`` between each
    # so synthetic / pretty-printed HTML matches too.
    r'\s*(?:<style[^>]*>[^<]*</style>\s*)*'
    r'<span[^>]*\bclass="css-[a-z0-9]+ [a-z0-9]+"[^>]*>(?P<label>[^<]+)</span>',
    re.DOTALL,
)
# Experience label: ``<span>· 3 - 7 Yrs of Exp</span>`` (also occasionally
# ``X+ Yrs of Exp``).
_EXPERIENCE_RE = re.compile(
    r'(?P<min>\d+)\s*(?:-\s*(?P<max>\d+)\s*)?(?:\+\s*)?Yrs?\s*of\s*Exp',
    re.IGNORECASE,
)
# Field label is the LAST ``css-o171kl`` anchor inside the card body
# whose href points at ``/a/{label}-Jobs-in-{Country}`` — e.g.
# ``Accounting/Finance``. The first ``css-o171kl`` is the title anchor;
# we filter on the ``/a/`` href to disambiguate. The label text often
# contains ``<!-- --> · <!-- -->`` separator comments — match the
# content lazily so they don't break the capture.
_FIELD_RE = re.compile(
    r'<a[^>]*class="css-o171kl"[^>]*href="(?:/[a-z-]+)?/a/(?P<slug>[^"]+?)-Jobs-in-[^"]+"[^>]*>(?P<label>.*?)</a>',
    re.DOTALL,
)
# Salary — Wuzzuf rarely exposes salary on listing cards (premium
# feature), but when present it ships verbatim inside a
# ``<span ... data-test="salary">`` or in a ``Salary: ...`` text node.
_SALARY_RE = re.compile(
    r'Salary[^A-Za-z0-9]*([A-Z]{3}|EGP|SAR|USD|EUR)?\s*([0-9][0-9,\s]+(?:\s*-\s*[0-9][0-9,\s]+)?)',
)

# Seniority-level slugs that share the ``css-o171kl`` class with the
# field anchor. We filter them out when picking the ``department``
# label so ``Accounting/Finance`` wins over ``Manager`` /
# ``Experienced``. Probed from Wuzzuf's listing: these are the only
# values that appear (matching the dropdown options on the search UI).
_LEVEL_SLUGS: frozenset[str] = frozenset({
    "Entry-Level", "Experienced", "Manager", "Senior-Management",
    "Director", "Intern", "Junior", "Mid-Level", "Senior",
})

# Employment-type labels → canonical enum. Wuzzuf renders these as
# ``/a/{slug}-Jobs-in-{Country}`` anchor text; the slug is what we
# match on (case-sensitive, hyphen-separated).
_EMPLOYMENT_BY_LABEL: dict[str, str] = {
    "Full-Time": "FULL_TIME",
    "Full Time": "FULL_TIME",
    "Part-Time": "PART_TIME",
    "Part Time": "PART_TIME",
    "Internship": "INTERN",
    "Intern": "INTERN",
    "Freelance": "CONTRACT",
    "Contract": "CONTRACT",
    "Contractor": "CONTRACT",
    "Project": "CONTRACT",
    "Shift-Based": "PART_TIME",
    "Temporary": "TEMPORARY",
    "Workshop": "TEMPORARY",
}

# Comment / tag stripper for cleaning location strings.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


@ScraperRegistry.register(ATSType.WUZZUF)
class WuzzufScraper(BaseScraper):
    """Wuzzuf (wuzzuf.net) — Egypt + Saudi Arabia direct postings.

    Single-source scraper: ``company_slug`` selects the country segment
    rather than a tenant. Supported values:

    - ``"egypt"`` (default — most volume, ~340 reachable rows)
    - ``"saudi-arabia"``
    - ``"all"`` — fan out across every segment in ``COUNTRY_SEGMENTS``

    Pass anything else to enumerate Egypt (so legacy ``"any"`` /
    ``""`` callers keep working).

    Knobs:

    - ``max_pages`` (default ``MAX_PAGES``) — hard cap on
      ``?start=`` iterations per country. The scraper also stops
      naturally when a page returns zero job links.
    """

    ats = ATSType.WUZZUF

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        max_pages: int = MAX_PAGES,
    ) -> None:
        super().__init__(company_slug, timeout=timeout)
        self.max_pages = max_pages

    # ``company_slug`` → list of country segment keys.
    def _resolve_countries(self) -> list[str]:
        key = (self.company_slug or "").strip().lower()
        if key == "all":
            return list(COUNTRY_SEGMENTS)
        if key in COUNTRY_SEGMENTS:
            return [key]
        # Legacy ``"any"`` / ``""`` / unknown → default to Egypt.
        return ["egypt"]

    def fetch(self) -> list[Job]:
        return asyncio.run(self._fetch_async())

    async def _fetch_async(self) -> list[Job]:
        countries = self._resolve_countries()
        seen: set[str] = set()
        jobs: list[Job] = []
        lock = asyncio.Lock()

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            cookies={"language": "en"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            sem = asyncio.Semaphore(MAX_CONCURRENCY)

            async def absorb(parsed: list[Job]) -> None:
                async with lock:
                    for job in parsed:
                        if job.ats_id is None or job.ats_id in seen:
                            continue
                        seen.add(job.ats_id)
                        jobs.append(job)

            async def per_country(country_key: str) -> None:
                url_prefix, filter_value, country_iso = COUNTRY_SEGMENTS[
                    country_key
                ]
                # Per-country wrap-around detector. The shared ``seen``
                # set is for output dedup only — using it for the stop
                # check would let a cross-listed ats_id (same posting on
                # two country mirrors) make this country stop early and
                # drop its remaining pages. Track ids this country has
                # already seen separately so each mirror paginates fully.
                country_seen: set[str] = set()
                # Wuzzuf paginates by ``start`` not ``page``: the offset
                # is the page index * PER_PAGE. Iterate sequentially —
                # we stop as soon as a page returns zero links, so
                # concurrent fan-out would waste requests past the cap.
                for page_idx in range(self.max_pages):
                    start = page_idx * PER_PAGE
                    html_text = await self._fetch_listing(
                        client, sem,
                        url_prefix=url_prefix,
                        filter_value=filter_value,
                        start=start,
                    )
                    parsed = _parse_listing(
                        html_text,
                        url_prefix=url_prefix,
                        country_iso=country_iso,
                    )
                    if not parsed:
                        return
                    # If this country has already seen every id on the
                    # page we've hit its wrap-around. Stop instead of
                    # spinning.
                    new_ids = [
                        job.ats_id
                        for job in parsed
                        if job.ats_id is not None
                        and job.ats_id not in country_seen
                    ]
                    if not new_ids:
                        return
                    country_seen.update(new_ids)
                    await absorb(parsed)

            await asyncio.gather(*(per_country(c) for c in countries))
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_listing(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        *,
        url_prefix: str,
        filter_value: str,
        start: int,
    ) -> str:
        url = f"{BASE_URL}{url_prefix}/search/jobs/"
        params = {
            "filters[country][0]": filter_value,
            "start": str(start),
        }
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            async with sem:
                try:
                    response = await client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt == MAX_RETRIES:
                        raise ScraperError(
                            f"Wuzzuf fetch failed for {url} "
                            f"(start={start}): {exc}"
                        ) from exc
                    await asyncio.sleep(RETRY_BASE_DELAY * attempt)
                    continue
            if response.status_code == 200:
                return response.text
            if response.status_code == 404:
                # ``/saudi/`` 404 if the country mirror has been retired;
                # treat as empty.
                return ""
            if response.status_code in (429,) or 500 <= response.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise ScraperError(
                        f"Wuzzuf returned {response.status_code} for {url} "
                        f"(start={start}) after {MAX_RETRIES} retries"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else RETRY_BASE_DELAY * (2 ** attempt)
                )
                await asyncio.sleep(delay)
                continue
            raise ScraperError(
                f"Wuzzuf returned {response.status_code} for {url} "
                f"(start={start})"
            )
        raise ScraperError(
            f"Wuzzuf exhausted retries for {url} (start={start}): {last_exc}"
        )


# --- HTML parsing -----------------------------------------------------------


def _parse_listing(
    html_text: str,
    *,
    url_prefix: str,
    country_iso: str,
) -> list[Job]:
    """Slice listing HTML into per-card blocks and parse each one.

    Wuzzuf renders each card as a self-contained ``<div class="css-ghe2tq
    e1v1l3u10"> ... </div>`` block; rather than depend on the obfuscated
    Emotion class name we split on the start of each card's posting
    ``<a href="/jobs/p/...">`` anchor and treat everything up to the
    next anchor (or end of page) as that card's body.
    """
    if not html_text:
        return []

    # Find every card-link match position in source order. We don't
    # bound the slice by the next regex match — postings include several
    # internal ``/jobs/p/`` references (apply CTA, share buttons) but
    # they all repeat the same id, so dedup by id within a card.
    matches = list(_JOB_LINK_RE.finditer(html_text))
    if not matches:
        return []

    seen_ids: set[str] = set()
    cards: list[tuple[str, str, str, str]] = []  # (id, href, title, card_html)

    for i, m in enumerate(matches):
        job_id = m.group("id")
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        # Card body extends from this match to the next *new* card's
        # match position. The block usually ends just before the next
        # ``css-ghe2tq`` div but slicing on the next link is robust.
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else min(len(html_text), m.start() + 6000)
        )
        # Walk forward to find the next *different* job id (some apply
        # CTAs link the same posting). If the next match shares this
        # id, extend through it.
        j = i + 1
        while j < len(matches) and matches[j].group("id") == job_id:
            end = (
                matches[j + 1].start()
                if j + 1 < len(matches)
                else min(len(html_text), matches[j].start() + 6000)
            )
            j += 1
        cards.append(
            (job_id, m.group("href"), m.group("title"), html_text[m.start():end])
        )

    now = datetime.now()
    jobs: list[Job] = []
    for job_id, href, title_raw, card_html in cards:
        job = _parse_card(
            job_id=job_id,
            href=href,
            title_raw=title_raw,
            card_html=card_html,
            url_prefix=url_prefix,
            country_iso=country_iso,
            now=now,
        )
        if job is not None:
            jobs.append(job)
    return jobs


def _parse_card(
    *,
    job_id: str,
    href: str,
    title_raw: str,
    card_html: str,
    url_prefix: str,
    country_iso: str,
    now: datetime,
) -> Job | None:
    title = _clean_text(title_raw)
    if not title:
        return None

    company_match = _COMPANY_RE.search(card_html)
    company = (
        _clean_text(company_match.group("company")) if company_match
        else "Unknown"
    ) or "Unknown"

    location = _extract_location(card_html)
    posted_at = _extract_posted_at(card_html, now=now)

    # Badge / employment_type / commitment. Each match has both a slug
    # (URL form, ``Full-Time``) and a label (display form, ``Full Time``).
    badge_pairs = [
        (m.group("slug"), _clean_text(m.group("label")))
        for m in _BADGE_RE.finditer(card_html)
    ]
    badge_slugs = [s for s, _ in badge_pairs]
    badge_labels = [lbl for _, lbl in badge_pairs if lbl]
    employment_type, commitment = _resolve_employment(badge_pairs)

    # Experience min — Wuzzuf surfaces a "min - max Yrs of Exp" range;
    # the canonical schema only carries ``experience`` (min), so the
    # max lives in raw.
    experience, experience_max = _extract_experience(card_html)

    # Field label (the LAST ``/a/{slug}-Jobs-in-...`` anchor inside the
    # card body that isn't an employment-type badge).
    field_label = _extract_field_label(card_html, badge_slugs=badge_slugs)

    salary_summary, salary_currency = _extract_salary(
        card_html, country_iso=country_iso,
    )

    # Build the canonical URL. Wuzzuf serves listings at the same
    # ``/jobs/p/{id}-...`` path regardless of the country segment in
    # the URL prefix — Egypt uses ``/jobs/p/``, Saudi uses
    # ``/saudi/jobs/p/``. Use the href as parsed.
    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    raw: dict[str, Any] = {}
    if experience_max is not None:
        raw["experience_max"] = experience_max
    if field_label:
        raw["field_label"] = field_label
    if badge_labels:
        # Persist the verbatim badge display labels in case downstream
        # wants the raw "Shift-Based" / "On-site" strings we don't map
        # to enums.
        raw["badges"] = badge_labels
    if commitment and not employment_type:
        raw["commitment_raw"] = commitment

    # Wuzzuf has a "Work from Home" / "Remote" badge; surface
    # ``is_remote=True`` when we see it (mirrors the canonical-schema
    # rule: only ever set True, never False, from a single keyword
    # check).
    is_remote: bool | None = None
    for slug in badge_slugs:
        sl = slug.lower()
        if sl in {"remote", "work-from-home", "telecommute", "fully-remote"}:
            is_remote = True
            break

    return Job(
        url=url,
        title=title,
        company=company,
        ats_type=ATSType.WUZZUF,
        ats_id=job_id,
        location=location,
        country_iso=country_iso,
        is_remote=is_remote,
        salary_summary=salary_summary,
        salary_currency=salary_currency,
        experience=experience,
        employment_type=employment_type,
        commitment=commitment,
        department=field_label,
        posted_at=posted_at,
        fetched_at=now,
        language="en",
        raw=raw or None,
    )


def _extract_location(card_html: str) -> str | None:
    m = _LOCATION_RE.search(card_html)
    if not m:
        return None
    text = _HTML_COMMENT_RE.sub("", m.group("loc"))
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # Collapse internal whitespace, then strip stray commas / spaces.
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(",").strip()
    return text or None


def _extract_posted_at(card_html: str, *, now: datetime) -> datetime | None:
    m = _POSTED_RE.search(card_html)
    if not m:
        return None
    return _parse_relative_time(m.group("posted"), now=now)


def _parse_relative_time(text: str, *, now: datetime) -> datetime | None:
    """Parse ``"X minutes ago"`` / ``"X hours ago"`` / ``"X days ago"`` /
    ``"X weeks ago"`` / ``"X months ago"`` / ``"X years ago"`` into a
    UTC-ish datetime. Wuzzuf renders all dates in this relative form on
    listing cards; the absolute timestamp only shows on the detail page
    (which we don't fetch).
    """
    cleaned = text.strip().lower()
    if not cleaned:
        return None

    m = re.match(
        r"(?P<n>\d+|a|an)\s+(?P<unit>minute|hour|day|week|month|year)s?\s+ago",
        cleaned,
    )
    if not m:
        return None

    n_token = m.group("n")
    n = 1 if n_token in {"a", "an"} else int(n_token)
    unit = m.group("unit")
    if unit == "minute":
        delta = timedelta(minutes=n)
    elif unit == "hour":
        delta = timedelta(hours=n)
    elif unit == "day":
        delta = timedelta(days=n)
    elif unit == "week":
        delta = timedelta(weeks=n)
    elif unit == "month":
        delta = timedelta(days=30 * n)
    elif unit == "year":
        delta = timedelta(days=365 * n)
    else:
        return None
    return now - delta


def _extract_experience(card_html: str) -> tuple[int | None, int | None]:
    m = _EXPERIENCE_RE.search(card_html)
    if not m:
        return None, None
    try:
        lo = int(m.group("min"))
    except (TypeError, ValueError):
        return None, None
    hi_raw = m.group("max")
    hi = int(hi_raw) if hi_raw and hi_raw.isdigit() else None
    return lo, hi


def _extract_field_label(
    card_html: str, *, badge_slugs: list[str]
) -> str | None:
    """Pick out the ``Accounting/Finance`` style category label.

    Card body anchors with ``class="css-o171kl"`` and href
    ``/a/{slug}-Jobs-in-{country}`` come in two flavours: the role
    level (``Manager`` / ``Experienced`` / ``Entry Level``) and the
    field (``Accounting/Finance``, ``IT/Software Development``). The
    level slug also shows up earlier as a job-level filter; the field
    is what we want for ``department``.

    Strategy: iterate every ``_FIELD_RE`` match in source order, skip
    the level (the FIRST anchor — its label is a single word like
    ``Manager`` and matches one of the canonical seniority slugs in
    ``_LEVEL_SLUGS``), and return the FIRST remaining anchor whose
    slug isn't an employment-type badge. This survives a re-ordering
    of the inner card layout.
    """
    badge_set = set(badge_slugs)
    for m in _FIELD_RE.finditer(card_html):
        slug = m.group("slug")
        if slug in badge_set or slug in _LEVEL_SLUGS:
            continue
        label = _clean_text(m.group("label"))
        # Listing labels often start with a ``· `` bullet separator —
        # strip it so the field name is bare (``Accounting/Finance``
        # not ``· Accounting/Finance``).
        label = label.lstrip("·").strip()
        if not label:
            continue
        return label
    return None


def _resolve_employment(
    badge_pairs: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Map Wuzzuf's employment-type badge → canonical enum + verbatim
    commitment label. The first badge that matches one of the keys in
    ``_EMPLOYMENT_BY_LABEL`` wins; later badges (``On-Site`` /
    ``Remote``) are positional / modality flags, not commitment.
    """
    for slug, label in badge_pairs:
        if slug in _EMPLOYMENT_BY_LABEL:
            return _EMPLOYMENT_BY_LABEL[slug], label or slug.replace("-", " ")
        norm = slug.replace("-", " ").strip()
        if norm in _EMPLOYMENT_BY_LABEL:
            return _EMPLOYMENT_BY_LABEL[norm], label or norm
        if label and label in _EMPLOYMENT_BY_LABEL:
            return _EMPLOYMENT_BY_LABEL[label], label
    return None, None


def _extract_salary(
    card_html: str, *, country_iso: str
) -> tuple[str | None, str | None]:
    """Wuzzuf hides salary on most cards (premium feature). When a
    ``Salary: ...`` string is present, capture it verbatim and tag with
    the local currency so downstream parse_salary_range can derive
    min/max.
    """
    m = _SALARY_RE.search(card_html)
    if not m:
        return None, None
    currency_token = (m.group(1) or "").upper().strip() or None
    amount = m.group(2).strip()
    summary = f"{currency_token + ' ' if currency_token else ''}{amount}"
    currency = currency_token or _CURRENCY_BY_ISO.get(country_iso)
    return summary, currency


def _clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = html.unescape(_TAG_RE.sub("", text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
