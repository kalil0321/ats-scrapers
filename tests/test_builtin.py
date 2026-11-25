"""Tests for the Built In scraper.

Pin the JSON-LD ItemList parsing (incl. the ``&#x2B;`` HTML-entity
trick Built In uses in the ``type`` attribute), the listing-only
default behaviour, and the guarantee that paid Firecrawl enrichment is
not part of this scraper.
"""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BuiltInScraper, ScraperRegistry

_LISTING_RE = re.compile(r"^https://builtin\.com/jobs\?page=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.builtin as bi
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


def test_preserves_html_in_description(httpx_mock) -> None:
    """Description now keeps HTML tags intact — the post-scrape
    markdownify pass converts them
    to markdown later. Entities are unescaped at scrape time.
    """
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(
            position=1, job_id=1, name="Engineer",
            description="<p>Build <b>things</b>&nbsp;today.</p>",
        )]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    j = BuiltInScraper("any").fetch()[0]
    assert "<b>things</b>" in j.description  # tags survive
    assert "&nbsp;" not in j.description     # entities decoded
    assert "&amp;" not in j.description


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


def test_waits_out_a_rate_limit_challenge(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 challenge mid-walk is retried; the pages after it are still read."""
    import ats_scrapers.scrapers.builtin as bi
    monkeypatch.setattr(bi, "MAX_RETRIES", 3)
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=100, name="A")]),
    )
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", status_code=429)
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=2",
        text=_listing_html([_item(position=1, job_id=200, name="B")]),
    )
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=3",
        text=_listing_html([_item(position=1, job_id=300, name="C")]),
    )

    jobs = BuiltInScraper("any", max_pages=3).fetch()

    assert [j.ats_id for j in jobs] == ["100", "200", "300"]


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even with all-fresh content, ``max_pages`` is the hard ceiling."""
    for p in range(1, 6):
        httpx_mock.add_response(
            url=f"https://builtin.com/jobs?page={p}",
            text=_listing_html([_item(position=1, job_id=p * 100, name=f"Job {p}")]),
        )
    jobs = BuiltInScraper("any", max_pages=5).fetch()
    assert len(jobs) == 5


# --- filter views -----------------------------------------------------------


def _board_state(
    *,
    remotes: tuple[str, ...] = ("remote", "office"),
    categories: tuple[str, ...] = ("sales",),
    experiences: tuple[str, ...] = ("senior",),
    sizes: tuple[str, ...] = ("1-10", "1000"),
) -> str:
    import json
    state = {
        "criteria": {"country": "USA", "pageSize": 10},
        "remotePreferenceFilterOptions": [{"id": i, "alias": a} for i, a in enumerate(remotes)],
        "categoriesFilterOptions": [{"id": i, "alias": a} for i, a in enumerate(categories)],
        "experiencesFilterOptions": [{"id": i, "alias": a} for i, a in enumerate(experiences)],
        "companySizesFilterOptions": [{"id": i, "alias": a} for i, a in enumerate(sizes)],
    }
    return (
        '<script type="module">\n        Builtin.jobBoardInit('
        f"{json.dumps(state)});\n    </script>"
    )


def _first_page(items: list[dict], **state: tuple[str, ...]) -> str:
    return _listing_html(items).replace("</body>", _board_state(**state) + "</body>")


def _view(path: str, page: int) -> str:
    return f"https://builtin.com/jobs/{path}?country=USA&page={page}"


def test_reads_every_filter_view_with_the_country(httpx_mock) -> None:
    """The unfiltered listing stops at page 1,000, so each remote / category /
    experience view is read too, with the country the view would otherwise
    take from the caller's IP."""
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([_item(position=1, job_id=1, name="Newest")]),
    )
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", text=_empty_page())
    httpx_mock.add_response(
        url=_view("remote/sales/senior", 1),
        text=_listing_html([
            _item(position=1, job_id=1, name="Newest"),
            _item(position=2, job_id=2, name="Remote seller"),
        ]),
    )
    httpx_mock.add_response(
        url=_view("remote/sales/senior", 2),
        text=_listing_html([_item(position=1, job_id=3, name="Older remote seller")]),
    )
    httpx_mock.add_response(url=_view("remote/sales/senior", 3), text=_empty_page())
    httpx_mock.add_response(
        url=_view("office/sales/senior", 1),
        text=_listing_html([_item(position=1, job_id=4, name="Office seller")]),
    )
    httpx_mock.add_response(url=_view("office/sales/senior", 2), text=_empty_page())

    jobs = BuiltInScraper("any", proxies=[None, "http://route-2:8080"]).fetch()

    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3", "4"]


def test_splits_a_view_still_full_at_the_last_page_by_company_size(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([], remotes=("office",)),
    )
    httpx_mock.add_response(
        url=_view("office/sales/senior", 1),
        text=_listing_html([_item(position=1, job_id=1, name="Capped")]),
    )
    httpx_mock.add_response(
        url=_view("office/sales/senior/1-10", 1),
        text=_listing_html([_item(position=1, job_id=2, name="Small company")]),
    )
    httpx_mock.add_response(
        url=_view("office/sales/senior/1000", 1),
        text=_listing_html([_item(position=1, job_id=3, name="Large company")]),
    )

    jobs = BuiltInScraper("any", max_pages=1).fetch()

    assert sorted(j.ats_id for j in jobs) == ["1", "2", "3"]


def test_reads_a_failed_view_page_again(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([], remotes=("office",)),
    )
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", text=_empty_page())
    httpx_mock.add_response(url=_view("office/sales/senior", 1), status_code=500)
    httpx_mock.add_response(
        url=_view("office/sales/senior", 1),
        text=_listing_html([_item(position=1, job_id=7, name="Second read")]),
    )
    httpx_mock.add_response(url=_view("office/sales/senior", 2), text=_empty_page())

    jobs = BuiltInScraper("any").fetch()

    assert [j.ats_id for j in jobs] == ["7"]


def test_a_direct_route_skips_the_environment_proxy(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ATS_SCRAPERS_PROXY", "http://env-proxy:3128")
    client_proxies: list[object] = []
    real_client = httpx.AsyncClient

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client_proxies.append(kwargs.get("proxy"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([_item(position=1, job_id=1, name="Newest")], remotes=("office",)),
    )
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", text=_empty_page())
    httpx_mock.add_response(url=_view("office/sales/senior", 1), text=_empty_page())

    BuiltInScraper("any", proxies=[None, "http://route-2:8080"]).fetch()

    # The first page is read on the first route, the direct one.
    assert client_proxies[0] is None
    assert set(client_proxies) <= {None, "http://route-2:8080"}


def test_time_budget_keeps_what_was_read(httpx_mock, caplog) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([_item(position=1, job_id=1, name="Newest")]),
    )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.builtin"):
        jobs = BuiltInScraper("any", max_seconds=0).fetch()

    assert [j.ats_id for j in jobs] == ["1"]
    assert "time budget ran out with 3 views unfinished" in caplog.text


# --- no paid enrichment -----------------------------------------------------


def test_never_calls_firecrawl_even_when_env_key_exists(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built In must remain listing-only. A leaked FIRECRAWL_API_KEY in
    cron/env must not turn on per-job paid enrichment."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-leaked-key")
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="X")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = BuiltInScraper("any").fetch()
    assert jobs[0].company == "Unknown"
    assert jobs[0].title == "X"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        BuiltInScraper("any").fetch()
