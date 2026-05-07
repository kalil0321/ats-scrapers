"""Tests for the We Work Remotely (WWR) scraper.

Pin the per-category fan-out (with cross-feed dedup), the
'Company: Title' splitting, and the structured-fields parsing. The
location-fallback logic ('Anywhere' is filtered, country/region/state
combine in the right order) gets its own coverage.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, WeWorkRemotelyScraper

_FEED_RE = re.compile(
    r"^https://weworkremotely\.com/categories/[a-z0-9-]+\.rss$"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.weworkremotely as wwr
    monkeypatch.setattr(wwr, "MAX_RETRIES", 1)
    monkeypatch.setattr(wwr, "RETRY_BASE_DELAY", 0.0)


def _empty_feed() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>WWR</title></channel></rss>'
    )


def _feed(items_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f'<title>WWR</title>{items_xml}'
        '</channel></rss>'
    )


def _item(
    *,
    guid: str,
    title: str,
    link: str | None = None,
    pub_date: str = "Wed, 06 May 2026 23:09:10 +0000",
    country: str = "United States",
    region: str = "",
    state: str = "",
    skills: str = "Python, Postgres",
    job_type: str = "Full-Time",
    description: str = "<p>Build a thing.</p>",
    expires_at: str = "",
) -> str:
    if link is None:
        link = f"https://weworkremotely.com/remote-jobs/{guid}"
    return (
        f'<item>'
        f'<title><![CDATA[{title}]]></title>'
        f'<link>{link}</link>'
        f'<guid isPermaLink="false">{guid}</guid>'
        f'<pubDate>{pub_date}</pubDate>'
        f'<country>{country}</country>'
        f'<region>{region}</region>'
        f'<state>{state}</state>'
        f'<skills>{skills}</skills>'
        f'<type>{job_type}</type>'
        f'<expires_at>{expires_at}</expires_at>'
        f'<description><![CDATA[{description}]]></description>'
        f'</item>'
    )


def _stub_all_categories_empty(httpx_mock) -> None:
    """Most tests only care about ONE category; stub the other 9 empty so
    asyncio.gather doesn't trip on un-stubbed requests."""
    httpx_mock.add_response(url=_FEED_RE, text=_empty_feed(), is_reusable=True)


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_wwr() -> None:
    assert ScraperRegistry.get(ATSType.WEWORKREMOTELY) is WeWorkRemotelyScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_item(httpx_mock) -> None:
    """Single item across all 10 category feeds (only one will populate)."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        text=_feed(_item(guid="praia-be-1", title="Praia Health: Senior Backend Engineer")),
    )
    _stub_all_categories_empty(httpx_mock)

    jobs = WeWorkRemotelyScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.WEWORKREMOTELY
    assert j.ats_id == "praia-be-1"
    assert j.company == "Praia Health"
    assert j.title == "Senior Backend Engineer"
    assert j.is_remote is True
    assert j.location == "United States"
    assert j.commitment == "Full-Time"
    assert j.description == "Build a thing."
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw.get("skills") == ["Python", "Postgres"]


# --- cross-feed dedup -------------------------------------------------------


def test_dedupes_jobs_present_in_multiple_categories(httpx_mock) -> None:
    """Some postings show up in 2 categories (e.g. 'remote-back-end' AND
    'remote-full-stack'). Same ``<guid>`` → single Job."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        text=_feed(_item(guid="x", title="Acme: Backend Eng")),
    )
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        text=_feed(_item(guid="x", title="Acme: Backend Eng")),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert len(jobs) == 1


# --- title parsing ----------------------------------------------------------


def test_company_title_split_on_colon(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-product-jobs.rss",
        text=_feed(_item(guid="p1", title="GitHub: Director of Product")),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].company == "GitHub"
    assert jobs[0].title == "Director of Product"


def test_title_without_colon_passes_through(httpx_mock) -> None:
    """Some postings don't use the 'Company: Title' format. Don't invent
    a fake company by splitting somewhere arbitrary."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/all-other-remote-jobs.rss",
        text=_feed(_item(guid="o1", title="Senior Designer wanted")),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].title == "Senior Designer wanted"
    # Empty company → "Unknown" placeholder so downstream queries don't
    # collapse on an empty string.
    assert jobs[0].company == "Unknown"


# --- location ---------------------------------------------------------------


def test_location_combines_state_country(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-design-jobs.rss",
        text=_feed(_item(
            guid="d1", title="Acme: Designer",
            country="United States", state="CA",
        )),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].location == "CA, United States"


def test_location_filters_anywhere_placeholder(httpx_mock) -> None:
    """``country='Anywhere'`` is WWR's 'global' placeholder; don't
    surface that as a real location string."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        text=_feed(_item(
            guid="c1", title="Acme: Support",
            country="Anywhere", region="Worldwide", state="",
        )),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].location == "Worldwide"


def test_state_set_drops_anywhere_country(httpx_mock) -> None:
    """Regression for the live-run bug: when WWR ships
    ``state='Alabama'`` + ``country='Anywhere in the World'``, the
    location should read 'Alabama' — the 'Anywhere…' is just WWR's
    'remote-eligible' tag and pollutes the location string when a real
    state is also set."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-product-jobs.rss",
        text=_feed(_item(
            guid="al1", title="Acme: PM",
            country="Anywhere in the World", state="Alabama", region="",
        )),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].location == "Alabama"


def test_anywhere_only_passes_through_as_location(httpx_mock) -> None:
    """When a posting truly is 'remote anywhere' with no state/region,
    we keep the 'Anywhere in the World' label so the row isn't blank."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-design-jobs.rss",
        text=_feed(_item(
            guid="aw1", title="Acme: Designer",
            country="Anywhere in the World", state="", region="",
        )),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert jobs[0].location == "Anywhere in the World"


# --- defensive --------------------------------------------------------------


def test_skips_item_without_guid_or_link(httpx_mock) -> None:
    """A malformed feed entry (missing guid or link) must not produce
    a half-built Job — drop it."""
    httpx_mock.add_response(
        url="https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        text=_feed(
            # Valid item
            _item(guid="ok", title="Acme: Sales")
            # Bad item — empty guid
            + '<item><title>x</title><link>https://x</link><guid></guid></item>'
            # Bad item — no link
            + '<item><title>y</title><link></link><guid>y</guid></item>'
        ),
    )
    _stub_all_categories_empty(httpx_mock)
    jobs = WeWorkRemotelyScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["ok"]


def test_malformed_feed_raises(httpx_mock) -> None:
    """If WWR ever stops returning well-formed XML (truncated stream,
    network glitch returning a partial body, etc.) the parse error
    should surface, not silently look like an empty category."""
    httpx_mock.add_response(
        url=_FEED_RE,
        text="<rss><channel><item><title>unclosed",
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="malformed"):
        WeWorkRemotelyScraper("any").fetch()


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_FEED_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        WeWorkRemotelyScraper("any").fetch()
