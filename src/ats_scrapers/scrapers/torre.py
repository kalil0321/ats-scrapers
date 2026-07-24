"""Torre.co (https://torre.ai) — LATAM-leaning direct-employer job platform.

Torre is a Colombian-founded job/talent platform with global reach,
especially strong across LATAM and remote roles. Companies post directly
(not aggregated from other sources), so coverage is high-signal:
~200k active opportunities worldwide at the time of writing — with
the bulk concentrated in Spanish-speaking Latin America, plus a long
remote/global tail in EN/PT.

Public POST search API at ``https://search.torre.co/opportunities/_search/``
— no auth, no key. The endpoint returns a fixed-shape envelope::

    {
        "total": 200534,
        "size": 25,
        "offset": 0,
        "results": [ ...opportunity objects... ],
        "pagination": {"previous": null, "next": "<base64-cursor>"}
    }

Pagination is **cursor-based** via ``?after=<token>`` even though the URL
also accepts ``offset=N`` — the latter is silently ignored at the server
side as of 2026-05, so the cursor is the only reliable forward-iterator.
Page size is capped server-side at around 30 per call (the response is
a 400 with ``meta.message: "Request size by <UA> too large: N"`` for
anything larger). We hard-code ``PAGE_SIZE = 25`` to stay well under
that bound across UA variants.

Single-source scraper: ``company_slug`` is informational and ignored.
Output rows carry the publishing employer's name as ``company`` so the
publisher's cross-ATS dedup still works.

Note: the search endpoint returns only a **summary** (``tagline``,
skills list, structured compensation) — the full posting body is not
included. The pipeline defers descriptions to :meth:`get_description`,
which extracts the full responsibilities body from the public posting
page and falls back to the search summary if that detail request fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.base import BaseScraper, ScraperRegistry

if TYPE_CHECKING:
    from typing import Any

    from ats_scrapers.fetch import Fetcher

SEARCH_URL = "https://search.torre.co/opportunities/_search/"
# Server caps page size around 30 for unauthenticated UAs (returns 400 with
# ``meta.message: "Request size by <UA> too large: N"``). 25 leaves a small
# safety margin for future tightening.
PAGE_SIZE = 25
MAX_RETRIES = 5
USER_AGENT = "Mozilla/5.0 (compatible; ats_scrapers/1.0; +https://stapply.ai)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# Canonical web URL for a posting (verified live 2026-07-24).
POSTING_URL = "https://torre.ai/post/{id}-{slug}"

# Torre exposes ``commitment`` as the human-readable label (``full-time``,
# ``part-time``, ``contract``, ``internship``, ``temporary``, ``freelance``).
# Map to the canonical employment_type enum.
_COMMITMENT_MAP: dict[str, str] = {
    "full-time": "FULL_TIME",
    "part-time": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "freelance": "CONTRACT",
    "internship": "INTERN",
    "intern": "INTERN",
    "temporary": "TEMPORARY",
}

# ``compensation.data.periodicity`` → canonical ``salary_period``. Torre uses
# lowercase singular nouns; the schema uses uppercase nouns.
_PERIOD_MAP: dict[str, str] = {
    "hourly": "HOUR",
    "daily": "DAY",
    "weekly": "WEEK",
    "monthly": "MONTH",
    "yearly": "YEAR",
    "annually": "YEAR",
}


@ScraperRegistry.register(ATSType.TORRE)
class TorreScraper(BaseScraper):
    """Torre.co (torre.ai) — direct-employer job platform.

    Single-source scraper: ``company_slug`` is ignored. Pass anything
    (``"any"``, ``""``, ``"latam"``) — the scraper enumerates the entire
    public opportunity feed via cursor-based pagination.
    """

    ats = ATSType.TORRE
    default_headers: ClassVar[dict[str, str]] = HEADERS

    def __init__(
        self,
        company_slug: str,
        *,
        timeout: float = 30.0,
        include_descriptions: bool = True,
        proxy: str | None = None,
        max_pages: int | None = None,
    ) -> None:
        super().__init__(
            company_slug,
            timeout=timeout,
            include_descriptions=include_descriptions,
            proxy=proxy,
        )
        if max_pages is not None and max_pages < 1:
            raise ScraperError(
                f"Torre max_pages must be positive, got {max_pages}"
            )
        self.max_pages = max_pages

    async def afetch(self) -> list[Job]:
        return await self._fetch_async()

    def fetch(self) -> list[Job]:
        return self._run_sync(self.afetch())

    def get_description(self, job: Job) -> str | None:
        fallback = job.description
        if job.raw:
            search_summary = job.raw.get("search_summary")
            if isinstance(search_summary, str) and search_summary.strip():
                fallback = search_summary.strip()

        async def run() -> str | None:
            try:
                async with self.make_fetcher(retries=MAX_RETRIES) as fetch:
                    detail_html = await fetch.get_text(str(job.url))
            except ScraperError:
                return fallback
            return _parse_detail_description(detail_html) or fallback

        return self._run_sync(run())

    async def _fetch_async(self) -> list[Job]:
        seen: set[str] = set()
        seen_cursors: set[str] = set()
        jobs: list[Job] = []
        raw_rows = 0
        reported_total: int | None = None
        async with self.make_fetcher(retries=MAX_RETRIES) as fetch:
            cursor: str | None = None
            page = 0
            while True:
                if self.max_pages is not None and page >= self.max_pages:
                    break
                payload = await self._fetch_page(fetch, cursor=cursor)
                page += 1
                if not isinstance(payload.get("results"), list):
                    raise ScraperError("Torre response omitted results list")
                total = payload.get("total")
                if not isinstance(total, int) or total < 0:
                    raise ScraperError("Torre response had invalid total")
                if reported_total is None:
                    reported_total = total
                    if reported_total == 0:
                        raise ScraperError(
                            "Torre full-catalogue response reported zero jobs"
                        )
                results = payload["results"]
                if not results:
                    if not jobs:
                        raise ScraperError(
                            "Torre full-catalogue scrape returned no jobs"
                        )
                    pagination = payload.get("pagination")
                    if (
                        isinstance(pagination, dict)
                        and pagination.get("next")
                    ):
                        raise ScraperError(
                            "Torre returned an empty page with a next cursor"
                        )
                    break
                if not all(isinstance(opp, dict) for opp in results):
                    raise ScraperError(
                        "Torre results contained a non-object row"
                    )
                parsed = [self._parse_job(opp) for opp in results]
                if any(job is None for job in parsed):
                    raise ScraperError(
                        f"Torre could not parse every row on page {page}"
                    )
                raw_rows += len(results)
                for job in parsed:
                    if job is None:
                        continue
                    ats_id = job.ats_id
                    if ats_id in seen:
                        continue
                    seen.add(ats_id)
                    jobs.append(job)
                # Cursor-based pagination. ``pagination.next`` is None when
                # we've reached the end. Some responses omit pagination
                # entirely when the result count is below page size; treat
                # that as "done" too.
                next_cursor = (
                    (payload.get("pagination") or {}).get("next")
                    if isinstance(payload.get("pagination"), dict) else None
                )
                if not next_cursor:
                    break
                if not isinstance(next_cursor, str):
                    raise ScraperError("Torre returned a non-string next cursor")
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise ScraperError(
                        f"Torre repeated pagination cursor {next_cursor!r}"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        if (
            self.max_pages is None
            and reported_total is not None
            and raw_rows < reported_total
        ):
            raise ScraperError(
                "Torre catalogue ended before the reported total "
                f"({raw_rows}/{reported_total})"
            )
        return jobs

    # --- HTTP layer ---------------------------------------------------------

    async def _fetch_page(
        self,
        fetch: Fetcher,
        *,
        cursor: str | None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "size": PAGE_SIZE,
            "aggregate": "false",
        }
        if cursor:
            params["after"] = cursor
        payload = await fetch.post_json(SEARCH_URL, params=params, json={})
        if not isinstance(payload, dict):
            raise ScraperError(
                f"Torre returned {type(payload).__name__}, expected object"
            )
        return payload

    # --- parsing ------------------------------------------------------------

    def _parse_job(self, opp: dict[str, Any]) -> Job | None:
        ats_id = str(opp.get("id") or "").strip()
        title = (opp.get("objective") or "").strip()
        slug = (opp.get("slug") or "").strip()
        if not ats_id or not title:
            return None

        url = POSTING_URL.format(id=ats_id, slug=slug or ats_id)

        orgs = opp.get("organizations") or []
        first_org: dict[str, Any] = (
            orgs[0] if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict)
            else {}
        )
        company = (first_org.get("name") or "").strip() or "Unknown"

        # Location: prefer ``place.location[].id`` (always populated when the
        # opportunity has any geographic grounding); fall back to the
        # top-level ``locations`` array. Multi-location postings are
        # comma-joined.
        place = opp.get("place") if isinstance(opp.get("place"), dict) else {}
        place_locs = (place or {}).get("location") or []
        location_names: list[str] = []
        country_iso: str | None = None
        lat: float | None = None
        lon: float | None = None
        if isinstance(place_locs, list) and place_locs:
            for entry in place_locs:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("id") or "").strip()
                if name:
                    location_names.append(name)
                cc = entry.get("countryCode")
                if isinstance(cc, str) and len(cc) == 2 and country_iso is None:
                    country_iso = cc.upper()
                if lat is None and lon is None:
                    e_lat, e_lon = entry.get("latitude"), entry.get("longitude")
                    if isinstance(e_lat, (int, float)) and isinstance(e_lon, (int, float)):
                        lat, lon = float(e_lat), float(e_lon)
        if not location_names:
            top_locs = opp.get("locations") or []
            if isinstance(top_locs, list):
                location_names = [
                    s.strip() for s in top_locs
                    if isinstance(s, str) and s.strip()
                ]
        location = ", ".join(location_names[:5]) if location_names else None

        # Compensation. Three observed shapes:
        #   - compensation: None  → no salary data
        #   - compensation: {data: None, ...}  → employer hid salary
        #   - compensation: {data: {code: "to-be-agreed", minAmount: 0, ...}}
        #     → employer marked it negotiable; min/max are 0, treat as unknown
        #   - compensation: {data: {code: "range", minAmount: 500, maxAmount: 1000, ...}}
        #     → real range, populate fields.
        comp_root = opp.get("compensation") if isinstance(opp.get("compensation"), dict) else {}
        comp_data = (comp_root or {}).get("data") if isinstance((comp_root or {}).get("data"), dict) else None
        salary_min: float | None = None
        salary_max: float | None = None
        salary_currency: str | None = None
        salary_period: str | None = None
        salary_summary: str | None = None
        if isinstance(comp_data, dict):
            min_amt = comp_data.get("minAmount")
            max_amt = comp_data.get("maxAmount")
            min_f = _to_positive_float(min_amt)
            max_f = _to_positive_float(max_amt)
            if min_f is not None or max_f is not None:
                salary_min = min_f
                salary_max = max_f
                cur = comp_data.get("currency")
                if isinstance(cur, str) and len(cur) == 3:
                    salary_currency = cur.upper()
                per = (comp_data.get("periodicity") or "").lower()
                salary_period = _PERIOD_MAP.get(per)  # type: ignore[assignment]
                # Build a human summary like "500 – 1000 USD / monthly"
                if salary_currency:
                    if min_f is not None and max_f is not None and min_f != max_f:
                        amounts = f"{_fmt(min_f)} – {_fmt(max_f)}"
                    elif max_f is not None:
                        amounts = _fmt(max_f)
                    elif min_f is not None:
                        amounts = f"from {_fmt(min_f)}"
                    else:
                        amounts = ""
                    if amounts:
                        salary_summary = (
                            f"{amounts} {salary_currency}"
                            + (f" / {per}" if per else "")
                        )

        # Employment type: map ``commitment`` (or fall back to ``type``).
        raw_commitment = (opp.get("commitment") or "").strip().lower()
        employment_type = _COMMITMENT_MAP.get(raw_commitment)
        commitment_label = opp.get("commitment") if isinstance(opp.get("commitment"), str) else None

        # Skills: keep only the names, capped at 15 to bound size.
        skill_names: list[str] = [
            s.get("name")
            for s in (opp.get("skills") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str) and s.get("name").strip()
        ]

        search_summary = _build_description(opp.get("tagline"), skill_names)
        description = search_summary if self.include_descriptions else None

        # ``raw`` overflow — keep verbatim the Torre-specific fields the
        # canonical schema can't represent.
        raw: dict[str, Any] = {}
        if search_summary:
            raw["search_summary"] = search_summary
        org_size = first_org.get("size")
        if isinstance(org_size, (int, float)):
            raw["organization_size"] = org_size
        org_pub = first_org.get("publicId")
        if isinstance(org_pub, str) and org_pub:
            raw["organization_public_id"] = org_pub
        if skill_names:
            raw["skills"] = skill_names[:15]
        for key in ("type", "opportunity"):
            v = opp.get(key)
            if isinstance(v, str) and v:
                raw[key] = v
        if isinstance(opp.get("external"), bool):
            raw["external"] = opp["external"]
        deadline = opp.get("deadline")
        if isinstance(deadline, str) and deadline:
            raw["deadline"] = deadline
        ac_details = (comp_root or {}).get("additionalCompensationDetails")
        if isinstance(ac_details, dict) and ac_details:
            raw["additional_compensation"] = ac_details

        # ``is_remote``: only ever set True (matches project pattern; never
        # claim on-site without a positive signal).
        remote_flag = bool(opp.get("remote")) or bool((place or {}).get("remote"))
        is_remote: bool | None = True if remote_flag else None

        return Job(
            url=url,
            title=title,
            company=company,
            ats_type=ATSType.TORRE,
            ats_id=ats_id,
            location=location,
            country_iso=country_iso,
            lat=lat,
            lon=lon,
            is_remote=is_remote,
            salary_currency=salary_currency,
            salary_period=salary_period,  # type: ignore[arg-type]
            salary_summary=salary_summary,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,  # type: ignore[arg-type]
            commitment=commitment_label,
            description=description,
            posted_at=_parse_iso(opp.get("created")),
            fetched_at=datetime.now(tz=UTC),
            raw=raw or None,
        )


def _to_positive_float(value: object) -> float | None:
    """Coerce to ``float`` only when strictly positive.

    Torre uses ``0.0`` as a sentinel for "no value" (paired with the
    ``code: "to-be-agreed"`` marker on negotiable comp). Treating 0 as
    a real bound would let nonsense ranges leak into the salary fields.
    """
    if isinstance(value, bool):  # bool is an int subclass — exclude.
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _fmt(value: float) -> str:
    """Strip trailing ``.0`` from whole-number floats for the summary."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_description(tagline: object, skills: list[str]) -> str | None:
    """The search endpoint exposes only a one-line ``tagline`` summary —
    not the full posting body. Concatenate the tagline with a bulleted
    skills list so the description field has *something* searchable;
    LLM enrichment downstream is expected to fetch the full body from
    ``url`` when richer text is needed.
    """
    parts: list[str] = []
    if isinstance(tagline, str) and tagline.strip():
        parts.append(tagline.strip())
    if skills:
        bullet = "\n".join(f"- {s}" for s in skills[:15])
        parts.append(f"Skills:\n{bullet}")
    if not parts:
        return None
    return "\n\n".join(parts)


def _parse_detail_description(detail_html: str) -> str | None:
    if not detail_html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ScraperError(
            "Torre detail parsing requires beautifulsoup4. Install with "
            "`pip install ats-scrapers[scrapers]`."
        ) from exc

    soup = BeautifulSoup(detail_html, "html.parser")
    responsibilities = soup.select_one(".opportunity-responsibilities__preview")
    if responsibilities is None:
        return None
    description = responsibilities.get_text("\n", strip=True)
    return description or None
