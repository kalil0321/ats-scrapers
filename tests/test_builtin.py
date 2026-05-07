"""Tests for the Built In scraper.

Pin the JSON-LD ItemList parsing (incl. the ``&#x2B;`` HTML-entity
trick Built In uses in the ``type`` attribute), the listing-only
default behaviour, and the opt-in Firecrawl enrichment shape.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import BuiltInScraper, ScraperRegistry

_LISTING_RE = re.compile(r"^https://builtin\.com/jobs\?page=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.builtin as bi
    monkeypatch.setattr(bi, "MAX_RETRIES", 1)
    monkeypatch.setattr(bi, "RETRY_BASE_DELAY", 0.0)


def _listing_html(items: list[dict], *, encoded_plus: bool = True) -> str:
    """Build a Built In listing HTML page with the JSON-LD ItemList
    embedded the same way the real site does."""
    payload = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "CollectionPage", "name": "Jobs", "url": "https://builtin.com/jobs"},
            {
                "@type": "ItemList",
                "name": "Top Tech Jobs",
                "numberOfItems": len(items),
                "itemListElement": items,
            },
        ],
    }
    import json
    body = json.dumps(payload)
    type_attr = "application/ld&#x2B;json" if encoded_plus else "application/ld+json"
    return f'<html><body><script type="{type_attr}">{body}</script></body></html>'


def _item(*, position: int, job_id: int, name: str, description: str = "Build things.") -> dict:
    return {
        "@type": "ListItem",
        "position": position,
        "name": name,
        "url": f"https://builtin.com/job/{name.lower().replace(' ', '-')}/{job_id}",
        "description": description,
    }


def _empty_page() -> str:
    return _listing_html([])


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_builtin() -> None:
    assert ScraperRegistry.get(ATSType.BUILTIN) is BuiltInScraper


# --- happy path -------------------------------------------------------------


def test_parses_listing_with_html_entity_type_attr(httpx_mock) -> None:
    """Built In serves ``type='application/ld&#x2B;json'`` (HTML-entity
    encoded '+'); the parser must handle that — naive ``\\+json`` regex
    misses it and silently returns 0 jobs."""
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([
            _item(position=1, job_id=9278414, name="Actuarial Associate"),
            _item(position=2, job_id=9269374, name="Account Executive"),
        ], encoded_plus=True),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(),
        is_reusable=True,
    )

    jobs = BuiltInScraper("any").fetch()
    assert len(jobs) == 2
    j = jobs[0]
    assert j.ats_type is ATSType.BUILTIN
    assert j.ats_id == "9278414"
    assert j.title == "Actuarial Associate"
    assert j.company == "Unknown"  # listing-only — not enriched
    assert j.description == "Build things."
    assert str(j.url) == "https://builtin.com/job/actuarial-associate/9278414"


def test_parses_listing_with_plain_plus_type_attr(httpx_mock) -> None:
    """Defensive: also accept the unencoded ``application/ld+json``
    spelling so the scraper survives a future Built In template change."""
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="Engineer")], encoded_plus=False),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    assert len(BuiltInScraper("any").fetch()) == 1


def test_strips_html_from_description(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(
            position=1, job_id=1, name="Engineer",
            description="<p>Build <b>things</b>.</p>",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = BuiltInScraper("any").fetch()[0]
    assert j.description == "Build things ."


def test_skips_items_with_missing_required_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([
            _item(position=1, job_id=1, name="Good"),
            {"@type": "ListItem", "position": 2, "name": "no url"},
            {"@type": "ListItem", "position": 3, "url": "https://builtin.com/job/no-name/2"},
            # url shape doesn't match /job/{slug}/{id}
            {"@type": "ListItem", "position": 4, "name": "weird url",
             "url": "https://example.com/x"},
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = BuiltInScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


# --- pagination -------------------------------------------------------------


def test_paginates_until_three_consecutive_empty_pages(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=100, name="A")]),
    )
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=2",
        text=_listing_html([_item(position=1, job_id=200, name="B")]),
    )
    # Pages 3, 4, 5 all return the same items as before (or empty) →
    # zero new ids → stop after 3 in a row.
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[3-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = BuiltInScraper("any", max_pages=20).fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even with all-fresh content, ``max_pages`` is the hard ceiling."""
    for p in range(1, 6):
        httpx_mock.add_response(
            url=f"https://builtin.com/jobs?page={p}",
            text=_listing_html([_item(position=1, job_id=p * 100, name=f"Job {p}")]),
        )
    jobs = BuiltInScraper("any", max_pages=5).fetch()
    assert len(jobs) == 5


# --- Firecrawl enrichment (opt-in) ------------------------------------------


def test_no_firecrawl_call_when_no_api_key(httpx_mock, monkeypatch) -> None:
    """The library default is direct fetch only — Firecrawl must not
    be hit unless the user explicitly opts in. (Test passes because
    httpx_mock errors on un-stubbed Firecrawl URLs.)"""
    # Make sure no env-var bleed-through enables enrichment.
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="X")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    # If the scraper called Firecrawl, httpx_mock would fail the test
    # with 'no response' for the firecrawl.dev URL.
    jobs = BuiltInScraper("any").fetch()
    assert jobs[0].company == "Unknown"


def test_firecrawl_enrichment_fills_company_location_salary(httpx_mock) -> None:
    """When the user passes a Firecrawl API key, each job's URL is
    POSTed to Firecrawl's /v1/scrape with an extraction schema and the
    returned fields fill in company/location/salary."""
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([
            _item(position=1, job_id=1, name="Backend Engineer"),
            _item(position=2, job_id=2, name="Designer"),
        ]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/scrape",
        json={"data": {"extract": {
            "company": "Acme",
            "location": "New York, NY",
            "salary_min": 120000,
            "salary_max": 180000,
        }}},
        is_reusable=True,
    )

    jobs = BuiltInScraper("any", firecrawl_api_key="fc-test").fetch()
    j = jobs[0]
    assert j.company == "Acme"
    assert j.location == "New York, NY"
    assert j.salary_min == 120000
    assert j.salary_max == 180000
    assert j.salary_currency == "USD"


def test_firecrawl_failure_falls_back_to_listing_data(httpx_mock) -> None:
    """If Firecrawl itself errors (rate-limited, bad key, network), the
    job keeps its listing-level fields rather than disappearing."""
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="X")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://api.firecrawl.dev/v1/scrape",
        status_code=500,
        is_reusable=True,
    )
    jobs = BuiltInScraper("any", firecrawl_api_key="fc-test").fetch()
    # Listing-level data preserved — only enrichment was lost.
    assert jobs[0].company == "Unknown"
    assert jobs[0].title == "X"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        BuiltInScraper("any").fetch()
