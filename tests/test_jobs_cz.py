"""Tests for the jobs.cz (Czech Republic) scraper.

Pin:

1. The parsing contract — every field the scraper claims to extract from
   an ``article.SearchResultCard`` (id, title, company, location, salary,
   modality tags, employment_type when present) round-trips through
   ``_parse_listing``.
2. Pagination via ``?page=N``, including the silent-clamp behaviour
   (an entire page of duplicates → stop walking the seed).
3. The location-seed fan-out + cross-seed dedup contract.
4. Defensive parsing: malformed/incomplete cards are skipped, missing
   bs4 raises a clear ``ScraperError``.
"""

from __future__ import annotations

import os
import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JobsCzScraper, ScraperRegistry, get_scraper


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jobs_cz as j
    monkeypatch.setattr(j, "MAX_RETRIES", 1)
    monkeypatch.setattr(j, "RETRY_BASE_DELAY", 0.0)


def _card(
    *,
    job_id: str,
    title: str,
    company: str = "Acme s.r.o.",
    location: str = "Praha",
    salary_tag: str | None = None,
    extra_tags: tuple[str, ...] = (),
    href: str | None = None,
) -> str:
    """Build one ``article.SearchResultCard`` matching the live HTML
    shape jobs.cz renders (2026-05-12 snapshot)."""
    href = href or f"https://www.jobs.cz/rpd/{job_id}/?searchId=abc&rps=99"
    tags_html: list[str] = []
    if salary_tag is not None:
        tags_html.append(
            f'<span class="Tag Tag--success Tag--small Tag--subtle">{salary_tag}</span>'
        )
    for t in extra_tags:
        tags_html.append(
            f'<span class="Tag Tag--neutral Tag--small Tag--subtle">{t}</span>'
        )
    tags_block = "".join(tags_html)
    return f"""
    <article class="SearchResultCard">
      <header class="SearchResultCard__header">
        <h2 data-test-ad-title="{title}" class="SearchResultCard__title">
          <a data-jobad-id="{job_id}" data-link="jd-detail"
             href="{href}" class="link-primary">{title}</a>
        </h2>
      </header>
      <div class="SearchResultCard__body">{tags_block}</div>
      <footer class="SearchResultCard__footer">
        <ul class="SearchResultCard__footerList">
          <li class="SearchResultCard__footerItem">
            <span translate="no">{company}</span>
          </li>
          <li data-test="serp-locality" class="SearchResultCard__footerItem">
            {location}
          </li>
        </ul>
      </footer>
    </article>
    """


def _page(cards: list[str]) -> str:
    body = "".join(cards)
    return (
        f"<!doctype html><html lang='cs'><body>"
        f"<main>{body}</main></body></html>"
    )


# --- registry ---------------------------------------------------------------


def test_registry_resolves_jobs_cz() -> None:
    assert ScraperRegistry.get(ATSType.JOBSCZ) is JobsCzScraper


def test_get_scraper_returns_jobs_cz() -> None:
    s = get_scraper("jobs_cz", "any")
    assert isinstance(s, JobsCzScraper)


def test_ats_type_value_is_jobs_cz() -> None:
    """The enum value is the snake-case identifier used in CSV columns
    and downstream filtering — don't let it drift to ``jobs.cz``,
    ``jobscz`` or similar."""
    assert ATSType.JOBSCZ.value == "jobs_cz"


def test_empty_location_seeds_disables_fetching() -> None:
    assert JobsCzScraper("any", location_seeds=()).fetch() == []


def test_max_pages_is_lower_bounded() -> None:
    assert JobsCzScraper("any", max_pages=0).max_pages == 1
    assert JobsCzScraper("any", max_pages=-10).max_pages == 1


# --- happy path -------------------------------------------------------------


def test_parses_full_card_with_salary_range(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([
            _card(
                job_id="2001162227",
                title="Account Manager (Praha)",
                company="Alma Career Czechia s.r.o.",
                location="Praha – Libeň",
                salary_tag="55 000 ‍–‍ 60 000 Kč",
                extra_tags=(
                    "Odpověď do 2 týdnů",
                    "Možnost občasné práce z domova",
                ),
            ),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    jobs = JobsCzScraper("any", location_seeds=("praha",)).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.JOBSCZ
    assert j.ats_id == "2001162227"
    assert j.title == "Account Manager (Praha)"
    assert j.company == "Alma Career Czechia s.r.o."
    assert j.location == "Praha – Libeň"
    assert j.country_iso == "CZ"
    assert j.language == "cs"
    # URL strips the volatile searchId/rps tracking query.
    assert str(j.url) == "https://www.jobs.cz/rpd/2001162227/"
    # Salary parsed out of the Czech "X 000 – Y 000 Kč" tag.
    assert j.salary_currency == "CZK"
    assert j.salary_period == "MONTH"
    assert j.salary_min == 55_000
    assert j.salary_max == 60_000
    assert j.salary_summary is not None
    assert "55 000" in j.salary_summary and "Kč" in j.salary_summary
    assert j.fetched_at.tzinfo is not None
    assert j.fetched_at.utcoffset() is not None
    # Modality tags come through verbatim in ``raw``.
    assert j.raw is not None
    assert "modality" in j.raw
    assert "Možnost občasné práce z domova" in j.raw["modality"]  # type: ignore[operator]


def test_parses_card_with_single_salary_value(httpx_mock) -> None:
    """Salary tags without a range (just "60 000 Kč") populate only
    ``salary_min`` and leave ``salary_max`` empty."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(
            job_id="2001119016", title="Junior konzultant", salary_tag="60 000 Kč",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.salary_min == 60_000
    assert j.salary_max is None
    assert j.salary_currency == "CZK"


def test_card_without_salary_leaves_compensation_blank(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(job_id="x1", title="Stavbyvedoucí")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.salary_currency is None
    assert j.salary_min is None
    assert j.salary_summary is None


def test_employment_type_inferred_from_tags(httpx_mock) -> None:
    """When a card carries an employment-label Tag (rare on listing but
    happens), map it to the canonical ``EmploymentType``."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(
            job_id="brig1",
            title="Brigáda na léto",
            extra_tags=("Brigáda",),
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.employment_type == "PART_TIME"


def test_employment_type_intern_maps_to_intern(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(
            job_id="stage1",
            title="Stáž v marketingu",
            extra_tags=("Stáž",),
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.employment_type == "INTERN"


def test_employment_type_dohoda_maps_to_contract(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(
            job_id="dpp1",
            title="Asistent administrativy",
            extra_tags=("Dohoda o provedení práce",),
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.employment_type == "CONTRACT"


# --- pagination -------------------------------------------------------------


def test_paginates_until_empty_page(httpx_mock) -> None:
    """Walk pages 1..N until the response renders no cards. Sequential
    walk because the silent pager clamp means fanned-out requests
    would waste round-trips."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(job_id=f"p1-{i}", title=f"Job {i}") for i in range(30)]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([_card(job_id=f"p2-{i}", title=f"Job {i}") for i in range(30)]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=3$"),
        text=_page([]),  # empty page → stop walking the seed.
    )
    jobs = JobsCzScraper(
        "any", location_seeds=("praha",), max_pages=10,
    ).fetch()
    assert len(jobs) == 60


def test_stops_when_page_is_all_duplicates(httpx_mock) -> None:
    """The pager silently clamps deep page numbers to the last available
    page. When the same ids reappear, we treat it as the end of the
    seed and move on."""
    cards = _page([_card(job_id=f"d{i}", title=f"X {i}") for i in range(30)])
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=cards,
    )
    # ``page=2`` repeats the same ids — clamp detected, walk halts.
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=cards,
    )
    jobs = JobsCzScraper(
        "any", location_seeds=("praha",), max_pages=10,
    ).fetch()
    assert len(jobs) == 30


def test_max_pages_truncates(httpx_mock) -> None:
    """``max_pages=1`` must hit the seed exactly once even if results
    look unbounded — if it issues a 2nd request, httpx_mock raises on
    the un-stubbed URL."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(job_id=f"x{i}", title="X") for i in range(30)]),
    )
    jobs = JobsCzScraper(
        "any", location_seeds=("praha",), max_pages=1,
    ).fetch()
    assert len(jobs) == 30


# --- seed fan-out & dedup ---------------------------------------------------


def test_dedupes_across_location_seeds(httpx_mock) -> None:
    """Praha + Brno return overlapping rows for the same nationwide
    posting. Cross-seed dedup must keep exactly one copy per
    ``data-jobad-id``."""
    shared = _card(job_id="shared", title="Remote engineer")
    praha_only = _card(job_id="p-only", title="Praha-only")
    brno_only = _card(job_id="b-only", title="Brno-only")
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([shared, praha_only]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/brno/$"),
        text=_page([shared, brno_only]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/brno/\?page=2$"),
        text=_page([]),
    )
    jobs = JobsCzScraper("any", location_seeds=("praha", "brno")).fetch()
    ats_ids = sorted(j.ats_id for j in jobs)
    assert ats_ids == ["b-only", "p-only", "shared"]


def test_seed_failure_is_skipped_not_fatal(httpx_mock) -> None:
    """A seed that errors out (e.g. persistent 500) must not blow up the
    whole run — subsequent seeds still contribute their rows."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/"),
        status_code=500,
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/brno/$"),
        text=_page([_card(job_id="b1", title="OK")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/brno/\?page=2$"),
        text=_page([]),
    )
    jobs = JobsCzScraper("any", location_seeds=("praha", "brno")).fetch()
    assert [j.ats_id for j in jobs] == ["b1"]


# --- defensive --------------------------------------------------------------


def test_skips_card_without_job_id(httpx_mock) -> None:
    """A card whose link is missing ``data-jobad-id`` is dropped rather
    than emitting a row with a UUID-fallback ``global_id``."""
    bad_card = """
    <article class="SearchResultCard">
      <header><h2><a class="link-primary" href="/rpd/x">No id</a></h2></header>
    </article>
    """
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([bad_card, _card(job_id="good", title="Good")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    jobs = JobsCzScraper("any", location_seeds=("praha",)).fetch()
    assert [j.ats_id for j in jobs] == ["good"]


def test_missing_company_falls_back_to_unknown(httpx_mock) -> None:
    """``Job.company`` is required — when a card has no employer node we
    emit ``"Unknown"`` rather than failing validation."""
    card_no_company = """
    <article class="SearchResultCard">
      <header>
        <h2 data-test-ad-title="Headless">
          <a data-jobad-id="nc1" href="/rpd/nc1/">Headless</a>
        </h2>
      </header>
      <footer>
        <ul><li data-test="serp-locality">Praha</li></ul>
      </footer>
    </article>
    """
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([card_no_company]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.company == "Unknown"


def test_persistent_500_on_seed_does_not_yield_jobs(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/"),
        status_code=500,
        is_reusable=True,
    )
    assert JobsCzScraper("any", location_seeds=("praha",)).fetch() == []


def test_404_returns_empty_silently(httpx_mock) -> None:
    """A seed slug that no longer exists 404s — treat as exhausted, not
    as a hard error. (Real-world: Czech cities reorganize occasionally
    and a slug may stop resolving.)"""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        status_code=404,
    )
    assert JobsCzScraper("any", location_seeds=("praha",)).fetch() == []


def test_salary_unparseable_keeps_summary_only(httpx_mock) -> None:
    """If the Tag--success text doesn't match the Kč regex (e.g.
    "Konkurenční plat"), keep the user-facing string but leave
    min/max blank."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/$"),
        text=_page([_card(
            job_id="ns1", title="X", salary_tag="Konkurenční plat",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.jobs\.cz/prace/praha/\?page=2$"),
        text=_page([]),
    )
    j = JobsCzScraper("any", location_seeds=("praha",)).fetch()[0]
    assert j.salary_min is None
    assert j.salary_max is None
    # Summary still surfaced so downstream enrichment can use it.
    assert j.salary_summary == "Konkurenční plat"
    assert j.salary_currency == "CZK"


# --- live e2e ---------------------------------------------------------------


def test_live_e2e_fetches_real_jobs_cz_page() -> None:
    """Opt-in smoke test against the real jobs.cz listing page.

    Normal CI keeps this skipped because it depends on the public site.
    Run with ``JOBHIVE_LIVE_E2E=1`` when reviewing the scraper PR.
    """
    if os.environ.get("JOBHIVE_LIVE_E2E") != "1":
        pytest.skip("set JOBHIVE_LIVE_E2E=1 to hit the real jobs.cz site")

    jobs = JobsCzScraper(
        "jobs_cz",
        location_seeds=("praha",),
        max_pages=1,
        timeout=20,
    ).fetch()

    assert jobs
    for job in jobs[:5]:
        assert job.ats_type is ATSType.JOBSCZ
        assert job.ats_id
        assert job.title
        assert job.company
        assert str(job.url).startswith("https://www.jobs.cz/")
        print(f"{job.title} | {job.company} | {job.location} | {job.url}")


# --- module-level helpers ---------------------------------------------------


def test_parse_salary_handles_zwj_and_nbsp() -> None:
    """The live HTML interleaves U+200D (zero-width joiner) and U+00A0
    (non-breaking space) into the salary text. The parser strips both
    before matching."""
    from jobhive.scrapers.jobs_cz import _normalize_whitespace, _parse_salary

    raw = "60 000 ‍–‍ 70 000 Kč"
    cleaned = _normalize_whitespace(raw)
    lo, hi, summary = _parse_salary(cleaned)
    assert lo == 60_000
    assert hi == 70_000
    assert summary is not None and "Kč" in summary


def test_missing_bs4_raises_scraper_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop bs4 from sys.modules and force a fresh import to fail — the
    parser must raise a clear ``ScraperError`` rather than crashing with
    an ``ImportError`` deep in the stack."""
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "bs4", raising=False)
    real_import = builtins.__import__

    def fake_import(name: str, *a: object, **k: object) -> object:
        if name == "bs4":
            raise ImportError("no bs4 for you")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from jobhive.scrapers.jobs_cz import _parse_listing

    with pytest.raises(ScraperError):
        _parse_listing("<html></html>")
