"""Tests for the Wuzzuf (Egypt + MENA) scraper.

Wuzzuf has no JSON API; the scraper depends on listing HTML being
structured the way it is in Nov 2026. Pin the parsing contract with
fixture HTML that mirrors the real ``css-ghe2tq`` card layout — that
includes the obfuscated Emotion class names, ``<!-- -->`` separator
comments, and inline ``<style>`` blocks Emotion injects between the
``<a>`` and the pill ``<span>`` of each badge.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, WuzzufScraper
from jobhive.scrapers.wuzzuf import (
    _parse_listing,
    _parse_relative_time,
)

_SEARCH_RE = re.compile(r"^https://wuzzuf\.net(?:/[a-z-]+)?/search/jobs/?")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop retry delay so error-handling tests finish in <1 s."""
    import jobhive.scrapers.wuzzuf as m
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


# --- HTML fixture builders --------------------------------------------------


def _build_card(
    *,
    job_id: str = "kjd2l3sxp5io",
    href_prefix: str = "",
    title: str = "Accounting Manager",
    company: str = "Backbone Business Support",
    company_slug: str = "backbone-business-support-egypt-143328",
    location: str = "Cairo, Egypt",
    posted: str = "52 minutes ago",
    employment_slug: str = "Full-Time",
    employment_label: str = "Full Time",
    modality_slug: str = "On-Site",
    modality_label: str = "On-site",
    level_slug: str = "Manager",
    level_label: str = "Manager",
    field_slug: str = "Accounting-Finance",
    field_label: str = "Accounting/Finance",
    experience_range: str = "7 - 12 Yrs of Exp",
    country_filter: str = "Egypt",
    a_prefix: str = "",
    job_slug_tail: str = "accounting-manager-backbone-business-support-cairo-egypt",
) -> str:
    """Build one ``<div class="css-ghe2tq e1v1l3u10">`` card mirroring the
    real Wuzzuf layout.

    Knobs:
    - ``href_prefix`` — ``""`` for Egypt cards (``/jobs/p/...``),
      ``"/saudi"`` for Saudi cards (``/saudi/jobs/p/...``).
    - ``a_prefix`` — analogous prefix for the badge / field anchor
      hrefs (``/a/...`` vs ``/saudi/a/...``).
    """
    job_href = f"{href_prefix}/jobs/p/{job_id}-{job_slug_tail}"
    employment_href = (
        f"{a_prefix}/a/{employment_slug}-Jobs-in-"
        f"{country_filter.replace(' ', '-')}"
    )
    modality_href = (
        f"{a_prefix}/a/{modality_slug}-Jobs-in-"
        f"{country_filter.replace(' ', '-')}"
    )
    level_href = (
        f"{a_prefix}/a/{level_slug}-Jobs-in-"
        f"{country_filter.replace(' ', '-')}"
    )
    field_href = (
        f"{a_prefix}/a/{field_slug}-Jobs-in-"
        f"{country_filter.replace(' ', '-')}"
    )
    # The ``<!-- -->`` separator comments are part of Wuzzuf's React
    # render output (server-side hydration markers). Keep them.
    return f"""
<div class="css-ghe2tq e1v1l3u10">
  <div class="css-pkv5jc">
    <h2 class="css-193uk2c">
      <a rel="noreferrer" class="css-o171kl" href="{job_href}" target="_blank">{title}</a>
    </h2>
    <div class="css-1k5ee52">
      <a href="https://wuzzuf.net/jobs/careers/{company_slug}" target="_blank" rel="noreferrer" class="css-ipsyv7">{company} -</a>
      <span class="css-16x61xq">{location.split(', ', 1)[0]}, <!-- -->{location.split(', ', 1)[1] if ', ' in location else ''} </span>
      <div class="css-eg55jf">{posted}</div>
    </div>
  </div>
  <div class="css-1rhj4yg">
    <div class="css-5jhz9n">
      <a class="css-a85cz4" href="{employment_href}">
        <style data-emotion="css nmaiir">.css-nmaiir{{}}</style>
        <style data-emotion="css uc9rga">.css-uc9rga{{}}</style>
        <span class="css-uc9rga eoyjyou0">{employment_label}</span>
      </a>
      <a href="{modality_href}">
        <style data-emotion="css 1d63l17">.css-1d63l17{{}}</style>
        <style data-emotion="css uofntu">.css-uofntu{{}}</style>
        <span class="css-uofntu eoyjyou0">{modality_label}</span>
      </a>
    </div>
    <div>
      <a class="css-o171kl" href="{level_href}">{level_label}</a>
      <span>· <!-- -->{experience_range}</span>
      <a class="css-o171kl" href="{field_href}"> <!-- -->· <!-- -->{field_label}</a>
    </div>
  </div>
</div>
""".strip()


def _build_listing(cards: list[str]) -> str:
    """Wrap cards in a minimal listing-page envelope."""
    return (
        "<!DOCTYPE html><html><head><title>x</title></head><body>"
        + "".join(cards)
        + "</body></html>"
    )


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_wuzzuf() -> None:
    assert ScraperRegistry.get(ATSType.WUZZUF) is WuzzufScraper


def test_country_slug_defaults_to_egypt() -> None:
    scraper = WuzzufScraper("anything-unknown")
    assert scraper._resolve_countries() == ["egypt"]
    assert WuzzufScraper("")._resolve_countries() == ["egypt"]
    assert WuzzufScraper("egypt")._resolve_countries() == ["egypt"]


def test_country_slug_all_fans_out() -> None:
    scraper = WuzzufScraper("all")
    countries = scraper._resolve_countries()
    assert set(countries) == {"egypt", "saudi-arabia"}


def test_country_slug_saudi_arabia() -> None:
    assert WuzzufScraper("saudi-arabia")._resolve_countries() == ["saudi-arabia"]


# --- parser unit tests ------------------------------------------------------


def test_parse_listing_extracts_full_card_payload() -> None:
    """Single Egypt card round-trips every populated Job field."""
    html_text = _build_listing([_build_card()])
    jobs = _parse_listing(html_text, url_prefix="", country_iso="EG")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.WUZZUF
    assert j.ats_id == "kjd2l3sxp5io"
    assert j.title == "Accounting Manager"
    assert j.company == "Backbone Business Support"
    assert j.location == "Cairo, Egypt"
    assert j.country_iso == "EG"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Full Time"
    assert j.department == "Accounting/Finance"
    assert j.experience == 7
    assert j.raw is not None
    assert j.raw["experience_max"] == 12
    assert j.raw["field_label"] == "Accounting/Finance"
    assert j.raw["badges"] == ["Full Time", "On-site"]
    assert j.language == "en"
    assert str(j.url) == (
        "https://wuzzuf.net/jobs/p/kjd2l3sxp5io-accounting-manager-"
        "backbone-business-support-cairo-egypt"
    )


def test_parse_listing_handles_saudi_url_prefix() -> None:
    """Saudi cards use a ``/saudi/jobs/p/...`` href and a ``/saudi/a/...``
    badge href. country_iso flips to SA and the URL preserves the
    country segment."""
    card = _build_card(
        job_id="3pdjanoevedp",
        href_prefix="/saudi",
        a_prefix="/saudi",
        title="F&B Cost Controller",
        company="Rua Taiba",
        company_slug="rua-taiba-company-for-hotels-saudi-arabia-143300",
        location="Riyadh, Saudi Arabia",
        country_filter="Saudi Arabia",
        job_slug_tail="fb-cost-controller-riyadh-saudi-arabia",
        field_slug="Accounting-Finance",
        field_label="Accounting/Finance",
    )
    jobs = _parse_listing(
        _build_listing([card]),
        url_prefix="/saudi",
        country_iso="SA",
    )
    assert len(jobs) == 1
    j = jobs[0]
    assert j.country_iso == "SA"
    assert "saudi/jobs/p/3pdjanoevedp" in str(j.url)
    assert j.department == "Accounting/Finance"
    assert j.employment_type == "FULL_TIME"


def test_parse_listing_marks_remote_badge() -> None:
    """The ``Remote`` badge flips ``is_remote=True``; ``On-site`` /
    ``Hybrid`` leave it None (canonical-schema rule: never assert False)."""
    remote = _build_card(
        job_id="abc1remote000",
        modality_slug="Remote",
        modality_label="Remote",
    )
    onsite = _build_card(
        job_id="abc2onsite000",
        modality_slug="On-Site",
        modality_label="On-site",
    )
    hybrid = _build_card(
        job_id="abc3hybrid000",
        modality_slug="Hybrid",
        modality_label="Hybrid",
    )
    jobs = _parse_listing(
        _build_listing([remote, onsite, hybrid]),
        url_prefix="",
        country_iso="EG",
    )
    remote_job = next(j for j in jobs if j.ats_id == "abc1remote000")
    onsite_job = next(j for j in jobs if j.ats_id == "abc2onsite000")
    hybrid_job = next(j for j in jobs if j.ats_id == "abc3hybrid000")
    assert remote_job.is_remote is True
    assert onsite_job.is_remote is None
    assert hybrid_job.is_remote is None


def test_parse_listing_dedupes_repeated_ids() -> None:
    """Wuzzuf occasionally repeats a posting's ``/jobs/p/{id}`` href
    inside the same card (apply CTA, share button) — dedup so we only
    emit one row per unique id."""
    card = _build_card()
    duplicate_link = (
        '<a href="/jobs/p/kjd2l3sxp5io-accounting-manager-backbone-business-'
        'support-cairo-egypt">Apply now</a>'
    )
    jobs = _parse_listing(
        _build_listing([card + duplicate_link]),
        url_prefix="",
        country_iso="EG",
    )
    assert len(jobs) == 1
    assert jobs[0].ats_id == "kjd2l3sxp5io"


def test_parse_listing_returns_empty_for_zero_card_page() -> None:
    """Past the pagination cap Wuzzuf returns a page with the chrome
    intact but no ``<div class='css-ghe2tq'>`` cards — the scraper
    must treat that as EOF, not crash."""
    empty_page = (
        "<!DOCTYPE html><html><body>"
        "<h1>No jobs match your filters.</h1>"
        "</body></html>"
    )
    assert _parse_listing(empty_page, url_prefix="", country_iso="EG") == []


def test_parse_listing_handles_missing_field_label() -> None:
    """Some cards have a level (``Manager``) but no field anchor. The
    department falls back to ``None`` instead of misclassifying the
    level as the field."""
    # Strip the field anchor by giving it the same slug as the level —
    # the de-duper drops it.
    card = _build_card(
        field_slug="Manager",
        field_label="Manager",  # forces the field to be filtered out
    )
    jobs = _parse_listing(_build_listing([card]), url_prefix="", country_iso="EG")
    assert len(jobs) == 1
    assert jobs[0].department is None


# --- relative-time parsing --------------------------------------------------


def test_parse_relative_time_minutes() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    out = _parse_relative_time("52 minutes ago", now=now)
    assert out == now - timedelta(minutes=52)


def test_parse_relative_time_hours_singular() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    assert _parse_relative_time("1 hour ago", now=now) == now - timedelta(hours=1)
    assert _parse_relative_time("an hour ago", now=now) == now - timedelta(hours=1)


def test_parse_relative_time_days() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    assert _parse_relative_time("3 days ago", now=now) == now - timedelta(days=3)


def test_parse_relative_time_garbage_returns_none() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    assert _parse_relative_time("just now", now=now) is None
    assert _parse_relative_time("", now=now) is None


# --- pagination -------------------------------------------------------------


def test_paginates_until_empty_page(httpx_mock) -> None:
    """The scraper increments ``start`` by PER_PAGE until a page emits
    zero ``/jobs/p/`` links. After that it stops."""
    # First page: 2 cards.
    page1 = _build_listing([
        _build_card(job_id="aaaaaaaaaaaa", title="Job 1"),
        _build_card(job_id="bbbbbbbbbbbb", title="Job 2"),
    ])
    # Second page: 1 card.
    page2 = _build_listing([_build_card(job_id="cccccccccccc", title="Job 3")])
    # Third page: zero cards → stop.
    page3 = "<html><body><h1>No jobs</h1></body></html>"

    httpx_mock.add_response(url=_SEARCH_RE, text=page1)
    httpx_mock.add_response(url=_SEARCH_RE, text=page2)
    httpx_mock.add_response(url=_SEARCH_RE, text=page3)

    jobs = WuzzufScraper("egypt", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == [
        "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc",
    ]


def test_paginates_stops_on_full_duplicate_page(httpx_mock) -> None:
    """If Wuzzuf wraps and re-serves the same 15 cards we already have,
    the scraper stops — otherwise it would spin in a loop until
    ``max_pages``."""
    page = _build_listing([
        _build_card(job_id="aaaaaaaaaaaa", title="Job 1"),
        _build_card(job_id="bbbbbbbbbbbb", title="Job 2"),
    ])
    # Same payload twice in a row — every id is a duplicate on the
    # second call.
    httpx_mock.add_response(url=_SEARCH_RE, text=page)
    httpx_mock.add_response(url=_SEARCH_RE, text=page)

    jobs = WuzzufScraper("egypt", max_pages=10).fetch()
    assert {j.ats_id for j in jobs} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_max_pages_caps_iteration(httpx_mock) -> None:
    """``max_pages`` is the safety cap. The scraper must stop after
    that many requests even if pages keep returning new cards."""
    page = _build_listing([_build_card(job_id="aaaaaaaaaaaa")])
    # Add way more responses than max_pages so the cap is what stops
    # the loop, not exhaustion. Two responses, max_pages=1.
    httpx_mock.add_response(url=_SEARCH_RE, text=page, is_reusable=True)
    jobs = WuzzufScraper("egypt", max_pages=1).fetch()
    assert len(jobs) == 1


# --- multi-country ----------------------------------------------------------


def test_all_country_slug_hits_egypt_and_saudi(httpx_mock) -> None:
    """``WuzzufScraper("all")`` fans out across every entry in
    ``COUNTRY_SEGMENTS``."""
    eg = _build_listing([_build_card(
        job_id="aaaaaaaaaaaa", title="Egypt job",
    )])
    sa = _build_listing([_build_card(
        job_id="bbbbbbbbbbbb", title="Saudi job",
        href_prefix="/saudi", a_prefix="/saudi",
        location="Riyadh, Saudi Arabia",
        country_filter="Saudi Arabia",
        job_slug_tail="saudi-job",
    )])
    empty = "<html><body></body></html>"

    httpx_mock.add_response(
        url=re.compile(r"^https://wuzzuf\.net/search/jobs/?"),
        text=eg,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://wuzzuf\.net/search/jobs/?"),
        text=empty,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://wuzzuf\.net/saudi/search/jobs/?"),
        text=sa,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://wuzzuf\.net/saudi/search/jobs/?"),
        text=empty,
    )

    jobs = WuzzufScraper("all", max_pages=10).fetch()
    countries = {j.country_iso for j in jobs}
    assert countries == {"EG", "SA"}


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """A real upstream outage must surface as ScraperError so cron
    treats it as failure rather than ``Wuzzuf has no jobs today``."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        WuzzufScraper("egypt").fetch()


def test_404_returns_empty_not_crash(httpx_mock) -> None:
    """``/saudi/`` 404 if Wuzzuf retires the country mirror. Treat as
    'no data' so an ``all`` fan-out keeps the other countries' rows."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=404, is_reusable=True)
    jobs = WuzzufScraper("saudi-arabia").fetch()
    assert jobs == []
