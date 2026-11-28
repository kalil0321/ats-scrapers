"""Tests for the Built In scraper.

Pin the JSON-LD ItemList parsing (incl. the ``&#x2B;`` HTML-entity
trick Built In uses in the ``type`` attribute), the job cards, the job
pages, and the guarantee that paid Firecrawl enrichment is not part of
this scraper.
"""

from __future__ import annotations

import re

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import BuiltInScraper, ScraperRegistry

_LISTING_RE = re.compile(r"^https://builtin\.com/jobs\?page=\d+$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.builtin as bi
    monkeypatch.setattr(bi, "MAX_RETRIES", 1)
    monkeypatch.setattr(bi, "RETRY_BASE_DELAY", 0.0)


def _listing_only(**kwargs: object) -> BuiltInScraper:
    return BuiltInScraper("any", include_descriptions=False, **kwargs)  # type: ignore[arg-type]


def _listing_html(items: list[dict], *, encoded_plus: bool = True, cards: str = "") -> str:
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
    return f'<html><body>{cards}<script type="{type_attr}">{body}</script></body></html>'


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

    jobs = _listing_only().fetch()
    assert len(jobs) == 2
    j = jobs[0]
    assert j.ats_type is ATSType.BUILTIN
    assert j.ats_id == "9278414"
    assert j.title == "Actuarial Associate"
    assert j.company == "Unknown"  # no card on the page
    # The ItemList description is Built In's summary, not the posting.
    assert j.description is None
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
    assert len(_listing_only().fetch()) == 1


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
    jobs = _listing_only().fetch()
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
    jobs = _listing_only(max_pages=20).fetch()
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

    jobs = _listing_only(max_pages=3).fetch()

    assert [j.ats_id for j in jobs] == ["100", "200", "300"]


def test_max_pages_caps_pagination(httpx_mock) -> None:
    """Even with all-fresh content, ``max_pages`` is the hard ceiling."""
    for p in range(1, 6):
        httpx_mock.add_response(
            url=f"https://builtin.com/jobs?page={p}",
            text=_listing_html([_item(position=1, job_id=p * 100, name=f"Job {p}")]),
        )
    jobs = _listing_only(max_pages=5).fetch()
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
    return f"https://builtin.com/jobs/{path}?allLocations=true&page={page}"


def test_reads_every_filter_view_in_every_country(httpx_mock) -> None:
    """The unfiltered listing stops at page 1,000 and holds US jobs only, so
    each remote / category / experience view is read too, for every country
    rather than the one the view would otherwise take from the caller's IP."""
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

    jobs = _listing_only(proxies=[None, "http://route-2:8080"]).fetch()

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

    jobs = _listing_only(max_pages=1).fetch()

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

    jobs = _listing_only().fetch()

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

    _listing_only(proxies=[None, "http://route-2:8080"]).fetch()

    # The first page is read on the first route, the direct one.
    assert client_proxies[0] is None
    assert set(client_proxies) <= {None, "http://route-2:8080"}


def test_time_budget_keeps_what_was_read(httpx_mock, caplog) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_first_page([_item(position=1, job_id=1, name="Newest")]),
    )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.builtin"):
        jobs = _listing_only(max_seconds=0).fetch()

    assert [j.ats_id for j in jobs] == ["1"]
    assert "time budget ran out with 3 views unfinished" in caplog.text


# --- job cards --------------------------------------------------------------


def _card(
    job_id: int,
    *,
    company: str = "Micron Technology",
    attendance: str = "In-Office",
    place: str = "Boise, ID, USA",
    places: tuple[str, ...] = (),
    pay: str | None = None,
) -> str:
    """A listing card, in the markup Built In serves (trimmed)."""
    if places:
        tooltip = "".join(
            f"&lt;div class=&#x27;text-truncate&#x27;&gt;{p}&lt;/div&gt;" for p in places
        )
        location = (
            '<span class="font-barlow text-gray-04 text-decoration-underline z-1 cursor-pointer" '
            'data-bs-toggle="tooltip" data-bs-placement="bottom" data-bs-html="true" '
            f'role="tooltip" aria-label="Job locations" data-bs-title="{tooltip}">'
            f"{len(places)} Locations\n                                    </span>"
        )
    else:
        location = f'<span class="font-barlow text-gray-04">{place}</span>'
    icon = (
        '<div class="d-flex justify-content-center align-items-center h-lg min-w-md">'
        '<i class="fa-regular fa-{} fs-xs text-pretty-blue"></i></div>\n'
        "                                "
    )
    pay_html = (
        f'<div class="d-flex align-items-start gap-sm">{icon.format("sack-dollar")}'
        f'<span class="font-barlow text-gray-04">{pay}</span></div>'
    ) if pay else ""
    return (
        f'<div id="job-card-{job_id}" data-id="job-card" x-data="jobTracking" '
        f'x-init="initObserver({job_id}, true)" class="job-bounded-responsive">'
        '<div class="left-side-tile-item-2"><a href="/company/acme" target="_blank" '
        f'data-id="company-title" data-builtin-track-job-id="{job_id}" '
        f'class="font-barlow fw-medium fs-xl"><span>{company}</span></a></div>'
        '<span x-show="!showSavedTag" x-cloak class="fs-xs fw-bold">'
        '<i class="fa-regular fa-clock fs-xs text-gray-03"></i>Reposted 35 Minutes Ago</span>'
        f'<div class="d-flex align-items-start gap-sm">{icon.format("house-building")}'
        f'<span class="font-barlow text-gray-04">{attendance}</span></div>'
        f'<div class="d-flex align-items-start gap-sm">{icon.format("location-dot")}'
        f"<div>{location}</div></div>"
        f"{pay_html}{pay_html}"
        '<div class="fs-sm fw-regular mb-md text-gray-04">A summary.</div></div>'
    )


def _one_page(httpx_mock, items: list[dict], cards: str) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1", text=_listing_html(items, cards=cards),
    )
    httpx_mock.add_response(url="https://builtin.com/jobs?page=2", text=_empty_page())


def test_card_gives_company_place_attendance_and_pay(httpx_mock) -> None:
    _one_page(
        httpx_mock,
        [_item(position=1, job_id=11, name="Planner"), _item(position=2, job_id=12, name="Rep")],
        _card(11, company="AT&amp;T", attendance="Remote", place="California, USA",
              pay="107K-183K Annually")
        + _card(12, company="Hilton", place="Zürich, CHE"),
    )

    planner, rep = _listing_only().fetch()

    assert planner.company == "AT&T"
    assert planner.location == "California, USA"
    assert [(p.label, p.country_iso) for p in planner.locations] == [("California, USA", "US")]
    assert planner.country_iso == "US"
    assert (planner.workplace_type, planner.is_remote) == ("remote", True)
    assert planner.salary_summary == "107K-183K Annually"
    assert rep.company == "Hilton"
    assert rep.country_iso == "CH"
    assert (rep.workplace_type, rep.is_remote) == ("onsite", False)
    assert rep.salary_summary is None


def test_card_with_several_places_lists_them_all(httpx_mock) -> None:
    _one_page(
        httpx_mock,
        [_item(position=1, job_id=11, name="Adjuster")],
        _card(11, attendance="Hybrid", places=("Mesa, AZ, USA", "Tempe, AZ, USA", "Berlin, DEU")),
    )

    (job,) = _listing_only().fetch()

    assert job.location == "Mesa, AZ, USA; Tempe, AZ, USA; Berlin, DEU"
    assert [p.country_iso for p in job.locations] == ["US", "US", "DE"]
    assert job.country_iso is None
    assert (job.workplace_type, job.is_remote) == ("hybrid", None)


def test_a_place_count_is_not_a_place(httpx_mock) -> None:
    _one_page(
        httpx_mock,
        [_item(position=1, job_id=11, name="Adjuster")],
        _card(11, place="3 Locations\n        "),
    )

    (job,) = _listing_only().fetch()

    assert job.location is None
    assert job.locations is None


@pytest.mark.parametrize(
    ("label", "workplace", "remote"),
    [
        ("In-Office", "onsite", False),
        ("Hybrid", "hybrid", None),
        ("Remote", "remote", True),
        ("Remote or Hybrid", None, True),
        ("In-Office or Remote", "onsite_or_remote", True),
        ("Somewhere", None, None),
    ],
)
def test_attendance_labels(label: str, workplace: str | None, remote: bool | None) -> None:
    from ats_scrapers.scrapers.builtin import _attendance

    assert _attendance(label) == (workplace, remote)


# --- job pages --------------------------------------------------------------

_POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Planner Scheduler",
    "description": "<p>Plan <b>builds</b>&nbsp;in Milpitas.</p><ul><li>ERP</li></ul>",
    "identifier": {"@type": "PropertyValue", "name": "Flex", "value": "11"},
    "baseSalary": {
        "@type": "MonetaryAmount", "currency": "USD",
        "value": {"@type": "QuantitativeValue", "minValue": 82500, "maxValue": 113500,
                  "unitText": "YEAR"},
    },
    "datePosted": "2026-10-16",
    "employmentType": "FULL_TIME",
    "hiringOrganization": {"@type": "Organization", "name": "Flex"},
    "jobLocation": {
        "@type": "Place",
        "address": {"@type": "PostalAddress", "addressCountry": "USA",
                    "addressLocality": "Milpitas", "addressRegion": "California",
                    "postalCode": "0"},
        "geo": {"@type": "GeoCoordinates", "latitude": 37.43, "longitude": -121.89},
    },
    "validThrough": "2026-12-28T01:21:57+00:00",
}


def _job_page(posting: dict) -> str:
    import json

    return (
        '<html><head><script type="application/ld&#x2B;json">'
        f'{json.dumps({"@context": "https://schema.org", "@graph": [posting]})}'
        "</script></head><body>Apply</body></html>"
    )


def _job(job_id: int = 11) -> Job:
    return Job(
        url=f"https://builtin.com/job/planner-scheduler/{job_id}", title="Planner Scheduler",
        company="Unknown", ats_type=ATSType.BUILTIN, ats_id=str(job_id),
    )


def test_get_description_reads_the_posting_on_the_job_page(httpx_mock) -> None:
    from datetime import UTC, datetime

    httpx_mock.add_response(
        url="https://builtin.com/job/planner-scheduler/11", text=_job_page(_POSTING),
    )
    job = _job()

    description = BuiltInScraper("any").get_description(job)

    assert description == job.description
    assert "<b>builds</b>" in description  # tags survive for the markdown pass
    assert "&nbsp;" not in description
    assert job.company == "Flex"
    assert job.posted_at == datetime(2026, 10, 16, tzinfo=UTC)
    assert job.application_deadline == datetime(2026, 12, 28, 1, 21, 57, tzinfo=UTC)
    assert job.employment_type == "FULL_TIME"
    assert (job.salary_currency, job.salary_min, job.salary_max, job.salary_period) == (
        "USD", 82500, 113500, "YEAR",
    )
    assert [(p.label, p.city, p.region, p.country_iso) for p in job.locations] == [
        ("Milpitas, California, USA", "Milpitas", "California", "US"),
    ]
    assert job.location == "Milpitas, California, USA"
    assert (job.lat, job.lon) == (37.43, -121.89)


def test_job_page_keeps_every_place_and_the_remote_flag(httpx_mock) -> None:
    places = [
        {"@type": "Place", "address": {"addressCountry": country, "addressLocality": city}}
        for city, country in (("Mesa", "USA"), ("London", "GBR"))
    ]
    httpx_mock.add_response(
        url="https://builtin.com/job/planner-scheduler/11",
        text=_job_page({**_POSTING, "jobLocation": places, "jobLocationType": "TELECOMMUTE"}),
    )
    job = _job()
    job.location = "Mesa, AZ, USA; London, GBR"
    job.country_iso = "US"  # what a card naming one US place gives

    BuiltInScraper("any").get_description(job)

    assert [(p.city, p.country_iso) for p in job.locations] == [("Mesa", "US"), ("London", "GB")]
    assert job.location == "Mesa, AZ, USA; London, GBR"  # the card's own wording stays
    assert job.country_iso is None
    assert job.is_remote is True
    assert job.lat is None


def test_a_day_that_does_not_exist_leaves_the_date_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/job/planner-scheduler/11",
        text=_job_page({**_POSTING, "datePosted": "2026-02-30"}),
    )
    job = _job()

    assert BuiltInScraper("any").get_description(job) is not None
    assert job.posted_at is None


def test_job_pages_go_through_the_callers_transport(httpx_mock) -> None:
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=_job_page(_POSTING))

    scraper = BuiltInScraper("any")
    scraper.transport = httpx.MockTransport(handler)

    assert scraper.get_description(_job()) is not None
    assert seen == ["https://builtin.com/job/planner-scheduler/11"]


def test_an_async_only_transport_fails_the_job_page_not_the_run(httpx_mock) -> None:
    import httpx

    class AsyncOnly(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

    scraper = BuiltInScraper("any")
    scraper.transport = AsyncOnly()

    with pytest.raises(ScraperError, match="BaseTransport"):
        scraper.get_description(_job())


def test_a_gone_job_page_gives_no_description(httpx_mock) -> None:
    httpx_mock.add_response(url="https://builtin.com/job/planner-scheduler/11", status_code=404)

    assert BuiltInScraper("any").get_description(_job()) is None


def test_a_rate_limited_job_page_is_read_again(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.builtin as bi

    monkeypatch.setattr(bi, "MAX_RETRIES", 3)
    httpx_mock.add_response(url="https://builtin.com/job/planner-scheduler/11", status_code=429)
    httpx_mock.add_response(
        url="https://builtin.com/job/planner-scheduler/11", text=_job_page(_POSTING),
    )

    assert BuiltInScraper("any").get_description(_job()) is not None


def test_a_job_page_refused_every_time_raises(httpx_mock) -> None:
    httpx_mock.add_response(url="https://builtin.com/job/planner-scheduler/11", status_code=503)

    with pytest.raises(ScraperError, match="503"):
        BuiltInScraper("any").get_description(_job())


def test_fetch_reads_every_job_page_after_the_listing(httpx_mock) -> None:
    _one_page(httpx_mock, [_item(position=1, job_id=11, name="Planner Scheduler")], _card(11))
    httpx_mock.add_response(
        url="https://builtin.com/job/planner-scheduler/11", text=_job_page(_POSTING),
    )

    (job,) = BuiltInScraper("any").fetch()

    assert job.company == "Micron Technology"  # the card's name stays
    assert job.description.startswith("<p>Plan")
    assert job.location == "Boise, ID, USA"


def test_time_budget_leaves_job_pages_unread(httpx_mock, caplog) -> None:
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="Newest")]),
    )

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.builtin"):
        (job,) = BuiltInScraper("any", max_pages=1, max_seconds=0).fetch()

    assert job.description is None
    assert "1 job pages unread" in caplog.text


def test_route_pool_lends_each_route_once_per_interval(monkeypatch) -> None:
    import ats_scrapers.scrapers.builtin as bi

    clock = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(bi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bi.time, "sleep", sleep)
    pool = bi._RoutePool(["a", "b"], 10.0)

    with pool.take() as first, pool.take() as second:
        pass
    clock[0] += 4
    with pool.take() as third:
        pass

    assert (first.route, second.route, third.route) == ("a", "b", "a")
    assert slept == [6.0]


def test_route_pool_spaces_the_next_read_from_the_last_retry(monkeypatch) -> None:
    import ats_scrapers.scrapers.builtin as bi

    clock = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(bi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bi.time, "sleep", sleep)
    monkeypatch.setattr(bi, "MAX_RETRIES", 2)
    monkeypatch.setattr(bi, "RETRY_BASE_DELAY", 8.0)
    answers = iter([(429, ""), (200, "page"), (200, "page")])
    scraper = BuiltInScraper("any", proxies=["http://route-1:8080"])
    monkeypatch.setattr(scraper, "_get_sync", lambda url, route: next(answers))

    with scraper._detail_routes.take() as lease:
        assert scraper._job_page("https://builtin.com/job/a/1", lease) == "page"
    with scraper._detail_routes.take() as lease:
        assert scraper._job_page("https://builtin.com/job/b/2", lease) == "page"

    # The retry started at 108; the next read waits one interval from there.
    assert slept == [8.0, pytest.approx(1 / bi.DETAIL_RATE)]


# --- no paid enrichment -----------------------------------------------------


def test_never_calls_firecrawl_even_when_env_key_exists(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked FIRECRAWL_API_KEY in cron/env must not turn on per-job
    paid enrichment."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-leaked-key")
    httpx_mock.add_response(
        url="https://builtin.com/jobs?page=1",
        text=_listing_html([_item(position=1, job_id=1, name="X")]),
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://builtin\.com/jobs\?page=[2-9]$"),
        text=_empty_page(), is_reusable=True,
    )
    jobs = _listing_only().fetch()
    assert jobs[0].company == "Unknown"
    assert jobs[0].title == "X"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_LISTING_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        _listing_only().fetch()
