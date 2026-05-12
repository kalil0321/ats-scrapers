"""Tests for the MyJobMag (pan-African direct-posting board) scraper.

Pin parsing of the shared HTML template that drives MyJobMag's five
regional properties (NG, GH, KE, UG, ZA). Coverage:

1. Region routing — ``company_slug`` selects the regional base URL and
   country_iso / language stamps.
2. Listing-card parsing — single-posting cards, rollup cards with
   ``sub-job-sec`` children, AdSense placeholder skips.
3. Pagination — dedup-driven termination (the site never returns
   empty), and the ``max_pages`` safety cap.
4. JSON-LD detail helper — ``parse_jsonld_job`` and
   ``normalize_employment_type`` for the optional enrichment path.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import MyJobMagScraper, ScraperRegistry, get_scraper
from jobhive.scrapers.myjobmag import (
    REGIONS,
    normalize_employment_type,
    parse_jsonld_job,
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.myjobmag as m
    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


# Mocked HTTP doesn't always exhaust every page the scraper would otherwise
# request — pagination terminates as soon as a page yields zero new ids, so
# unused page=N mocks are expected.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


def _card(
    *,
    slug: str,
    title: str,
    company: str,
    description: str = "We are a great company.",
    date: str = "11 May",
    use_logo_alt: bool = True,
) -> str:
    """Render one single-job MyJobMag listing card."""
    logo = (
        f'<li class="job-logo">'
        f'<a href="/jobs-at/{company.lower().replace(" ", "-")}">'
        f'<img src="/x.png" alt="{company if use_logo_alt else ""}" />'
        f'</a></li>'
    )
    return (
        '<li class="job-list-li">'
        '<ul>'
        f'{logo}'
        '<li class="job-info"><ul>'
        '<li class="mag-b">'
        f'<h2><a style=" " href="/job/{slug}">{title} at {company}</a></h2>'
        '</li>'
        f'<li class="job-desc">{description}</li>'
        '<li class="job-item"><ul>'
        f'<li id="job-date">{date}</li>'
        '</ul></li>'
        '</ul></li>'
        '</ul></li>'
    )


def _rollup_card(
    *,
    rollup_slug: str,
    rollup_title: str,
    company: str,
    sub_jobs: list[tuple[str, str]],
    date: str = "11 May",
) -> str:
    """Render a 'Latest Jobs at <Company>' rollup with N sub-job links."""
    sub_html = "".join(
        f'<li><a href="/job/{slug}">{t}</a></li>' for slug, t in sub_jobs
    )
    return (
        '<li class="job-list-li">'
        '<ul>'
        f'<li class="job-logo">'
        f'<a href="/jobs-at/{company.lower().replace(" ", "-")}">'
        f'<img src="/x.png" alt="{company}" />'
        f'</a></li>'
        '<li class="job-info"><ul>'
        '<li class="mag-b">'
        f'<h2><a style=" " href="/jobs/latest-jobs-at-{rollup_slug}">'
        f'{rollup_title}</a></h2>'
        '</li>'
        '<li class="job-desc">Company description.</li>'
        '<li class="job-item"><ul>'
        f'<li id="job-date">{date}</li>'
        '</ul></li>'
        '<li class="sub-job-sec">'
        f'<ul id="sbu-job-list">{sub_html}</ul>'
        '</li>'
        '</ul></li>'
        '</ul></li>'
    )


def _ad_card() -> str:
    """An AdSense placeholder card — same outer wrapper, no job-info."""
    return (
        '<li class="job-list-li" style="text-align:center;">'
        '<div id="adbox">ads</div>'
        '</li>'
    )


def _page(cards: list[str]) -> str:
    return (
        '<!doctype html><html><body>'
        '<ul id="job-list">'
        + "".join(cards) +
        '</ul></body></html>'
    )


def _url(base: str, page: int) -> str:
    return f"{base}/jobs/page/{page}"


# --- Registry / wiring ------------------------------------------------------


def test_registry_resolves_myjobmag() -> None:
    assert ScraperRegistry.get(ATSType.MYJOBMAG) is MyJobMagScraper


def test_get_scraper_by_string() -> None:
    s = get_scraper("myjobmag", "ke")
    assert isinstance(s, MyJobMagScraper)
    assert s.company_slug == "ke"


# --- Region routing ---------------------------------------------------------


@pytest.mark.parametrize(
    "slug, expected_base, expected_iso",
    [
        ("ng", "https://www.myjobmag.com", "NG"),
        ("gh", "https://www.myjobmagghana.com", "GH"),
        ("ke", "https://www.myjobmag.co.ke", "KE"),
        ("ug", "https://www.myjobmag.co.ug", "UG"),
        ("za", "https://www.myjobmag.co.za", "ZA"),
    ],
)
def test_region_slug_maps_to_base_url_and_country_iso(
    slug: str, expected_base: str, expected_iso: str,
) -> None:
    s = MyJobMagScraper(slug)
    assert s._base_url == expected_base
    assert s._country_iso == expected_iso
    assert s._language == "en"


def test_empty_slug_defaults_to_nigeria() -> None:
    s = MyJobMagScraper("")
    assert s._country_iso == "NG"


@pytest.mark.parametrize("alias", ["any", "nigeria", "NIGERIA", "  ng  "])
def test_alias_routes_to_nigeria(alias: str) -> None:
    assert MyJobMagScraper(alias)._country_iso == "NG"


def test_unknown_region_raises() -> None:
    with pytest.raises(ScraperError):
        MyJobMagScraper("mars")


def test_all_regions_in_map() -> None:
    """Pin the five-country contract — losing a region is a breaking
    change for downstream company-CSV consumers."""
    assert set(REGIONS) == {"ng", "gh", "ke", "ug", "za"}


# --- Listing-card parsing ---------------------------------------------------


def test_parses_single_job_card(httpx_mock) -> None:
    cards = [
        _card(
            slug="financial-manager-acme-1",
            title="Financial Manager",
            company="Acme",
            description="Great role at Acme.",
        ),
    ]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    jobs = MyJobMagScraper("ng").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.MYJOBMAG
    assert j.ats_id == "financial-manager-acme-1"
    assert j.title == "Financial Manager"
    assert j.company == "Acme"
    assert j.country_iso == "NG"
    assert j.language == "en"
    assert str(j.url) == "https://www.myjobmag.com/job/financial-manager-acme-1"
    assert j.description == "Great role at Acme."
    assert j.posted_at is not None


def test_skips_adsense_placeholder_cards(httpx_mock) -> None:
    cards = [
        _ad_card(),
        _card(slug="x-acme", title="X", company="Acme"),
        _ad_card(),
    ]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    jobs = MyJobMagScraper("ng").fetch()
    assert [j.ats_id for j in jobs] == ["x-acme"]


def test_rollup_card_emits_sub_jobs_not_rollup(httpx_mock) -> None:
    """``/jobs/latest-jobs-at-acme`` rollups expand to one Job per
    sub-job link; the rollup URL itself (with the ``/jobs/`` prefix,
    not ``/job/``) is never emitted as a posting."""
    cards = [_rollup_card(
        rollup_slug="acme",
        rollup_title="Latest Jobs at Acme",
        company="Acme",
        sub_jobs=[
            ("backend-engineer-acme", "Backend Engineer"),
            ("frontend-engineer-acme", "Frontend Engineer"),
            ("data-analyst-acme", "Data Analyst"),
        ],
    )]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    jobs = MyJobMagScraper("ng").fetch()
    ats_ids = sorted(j.ats_id or "" for j in jobs)
    assert ats_ids == [
        "backend-engineer-acme",
        "data-analyst-acme",
        "frontend-engineer-acme",
    ]
    # All sub-jobs inherit the rollup company.
    assert all(j.company == "Acme" for j in jobs)
    # Sub-job urls point at the singleton ``/job/`` path on the same
    # regional base.
    assert all(str(j.url).startswith("https://www.myjobmag.com/job/")
               for j in jobs)


def test_title_at_company_parsing(httpx_mock) -> None:
    """The listing renders titles as ``Title at Company`` — splitting
    on the LAST ``" at "`` keeps titles that themselves contain ``at``
    (e.g. 'Sales at Heart') intact."""
    cards = [
        _card(slug="x", title="Sales at Heart", company="Acme Corp"),
    ]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    j = MyJobMagScraper("ng").fetch()[0]
    assert j.title == "Sales at Heart"
    assert j.company == "Acme Corp"


def test_falls_back_to_logo_alt_when_title_has_no_at(httpx_mock) -> None:
    """Some titles ship as just ``Senior Engineer`` with no ``at``
    suffix — fall back to the company logo's ``alt`` attribute."""
    # Hand-craft a card without "at <Company>" in the h2.
    card = (
        '<li class="job-list-li"><ul>'
        '<li class="job-logo"><a href="/jobs-at/acme">'
        '<img src="/x.png" alt="Acme Industries" />'
        '</a></li>'
        '<li class="job-info"><ul>'
        '<li class="mag-b">'
        '<h2><a href="/job/standalone-role">Standalone Role</a></h2>'
        '</li>'
        '<li class="job-desc">Doing things.</li>'
        '<li class="job-item"><ul><li id="job-date">11 May</li></ul></li>'
        '</ul></li></ul></li>'
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page([card]),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    j = MyJobMagScraper("ng").fetch()[0]
    assert j.title == "Standalone Role"
    assert j.company == "Acme Industries"


# --- Pagination -------------------------------------------------------------


def test_paginates_until_zero_new_ids(httpx_mock) -> None:
    """Listing pages never go empty in practice, so termination is
    'this page introduced zero new ats_ids' rather than 'empty page'.
    Verify the scraper requests page=2 when page=1 has fresh ids, and
    stops when page=2 repeats them."""
    page1 = _page([
        _card(slug="a", title="A", company="Acme"),
        _card(slug="b", title="B", company="Acme"),
    ])
    # Page 2 repeats the same ids — should terminate, not crash.
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=page1,
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=page1,
    )
    jobs = MyJobMagScraper("ng").fetch()
    assert sorted(j.ats_id or "" for j in jobs) == ["a", "b"]


def test_paginates_through_multiple_pages_with_new_ids(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1),
        text=_page([_card(slug=f"p1_{i}", title="T", company="C") for i in range(3)]),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2),
        text=_page([_card(slug=f"p2_{i}", title="T", company="C") for i in range(2)]),
    )
    # Page 3 — no new ids.
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 3),
        text=_page([_card(slug="p1_0", title="T", company="C")]),
    )
    jobs = MyJobMagScraper("ng").fetch()
    assert len(jobs) == 5


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """With ``max_pages=1``, page=2 must never be requested even if
    page=1 brought new ids."""
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1),
        text=_page([_card(slug="a", title="A", company="C")]),
    )
    # If the scraper requested page=2, httpx_mock would NOT have a
    # response for it and the call would raise — we rely on that to
    # assert the cap holds.
    jobs = MyJobMagScraper("ng", max_pages=1).fetch()
    assert [j.ats_id for j in jobs] == ["a"]


# --- Defensive --------------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.myjobmag\.com/"),
        status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        MyJobMagScraper("ng").fetch()


def test_empty_page_terminates(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page([]),
    )
    assert MyJobMagScraper("ng").fetch() == []


# --- JSON-LD detail helper --------------------------------------------------
#
# The default fetch path is listing-only for throughput, but the
# detail-page JSON-LD JobPosting block carries richer fields
# (employmentType, postal address, occupationalCategory). Pin the
# helper used by downstream enrichment.


_DETAIL_PAGE = """
<html><head></head><body>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "title": "Financial Manager",
  "datePosted": "2026-05-11T16:13:34+01:00",
  "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
  "employmentType": "Full Time , Onsite",
  "jobLocation": {
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Lagos",
      "addressCountry": "NG"
    }
  }
}
</script>
</body></html>
"""


def test_parse_jsonld_job_returns_dict_when_present() -> None:
    obj = parse_jsonld_job(_DETAIL_PAGE)
    assert obj is not None
    assert obj["@type"] == "JobPosting"
    assert obj["title"] == "Financial Manager"
    assert obj["hiringOrganization"]["name"] == "Acme Corp"


def test_parse_jsonld_job_returns_none_when_absent() -> None:
    assert parse_jsonld_job("<html><body>no jsonld</body></html>") is None


def test_parse_jsonld_tolerates_unescaped_newlines() -> None:
    """Live MyJobMag detail pages embed raw newlines inside the
    ``description`` string (technically invalid JSON). The strict-mode
    parse fails; the helper retries with ``strict=False`` so real
    fixtures don't get dropped on the floor."""
    text = (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","title":"X",'
        '"description":"line1\nline2\nline3"}'
        '</script>'
    )
    obj = parse_jsonld_job(text)
    assert obj is not None
    assert obj["title"] == "X"


def test_parse_jsonld_skips_non_jobposting() -> None:
    """Some MyJobMag pages embed an Organization JSON-LD block but
    not a JobPosting — skip those rather than mis-typing them."""
    text = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"organization","name":"X"}'
        '</script>'
    )
    assert parse_jsonld_job(text) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Full Time", "FULL_TIME"),
        ("FULL_TIME", "FULL_TIME"),
        ("Full Time , Onsite", "FULL_TIME"),  # comma-joined tags
        ("Part Time", "PART_TIME"),
        ("Contract", "CONTRACT"),
        ("Internship", "INTERN"),
        ("Temporary", "TEMPORARY"),
        ("Full-Time/Contract", "FULL_TIME"),
        (["Full Time", "Onsite"], "FULL_TIME"),
        (None, None),
        ("Unknown Tag", None),
    ],
)
def test_normalize_employment_type(
    raw: Any, expected: str | None,
) -> None:
    assert normalize_employment_type(raw) == expected


# --- Posted-date inference -------------------------------------------------


def test_posted_at_binds_to_current_year_when_not_future(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing date shows day-month only (``11 May``). Without a
    year we attribute to the most-recent occurrence that isn't in the
    future, which keeps day-old posts from being mis-aged by 364 days
    around year boundaries."""
    # Stub ``datetime.now`` so the test is deterministic. The module
    # imports ``datetime`` from the stdlib by name, so patch at the
    # consumption site.
    import jobhive.scrapers.myjobmag as m

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2026, 5, 12, 10, 0, 0)
    monkeypatch.setattr(m, "datetime", _FrozenDT)

    cards = [_card(slug="x", title="X", company="C", date="11 May")]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    j = MyJobMagScraper("ng").fetch()[0]
    assert j.posted_at == datetime(2026, 5, 11)


def test_posted_at_backs_to_previous_year_for_future_month(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If today is 12 Jan 2026 and the listing says ``31 Dec``, the
    candidate (31 Dec 2026) is months in the future — back off to
    2025 so 'days-since-posting' stays small for a 12-day-old row."""
    import jobhive.scrapers.myjobmag as m

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2026, 1, 12, 10, 0, 0)
    monkeypatch.setattr(m, "datetime", _FrozenDT)

    cards = [_card(slug="x", title="X", company="C", date="31 Dec")]
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 1), text=_page(cards),
    )
    httpx_mock.add_response(
        url=_url("https://www.myjobmag.com", 2), text=_page([]),
    )
    j = MyJobMagScraper("ng").fetch()[0]
    assert j.posted_at == datetime(2025, 12, 31)
