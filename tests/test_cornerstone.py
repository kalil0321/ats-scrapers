"""Tests for the Cornerstone OnDemand scraper.

Cornerstone is a 2-step flow: extract a JWT token from the career-site HTML,
then POST to the regional ``api.csod.com`` host with the token. These tests
pin token extraction, region detection, pagination, and field parsing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import CornerstoneScraper, ScraperRegistry

CAREER_URL = "https://acme.csod.com/ux/ats/careersite/1/home?c=acme"
API_URL = "https://eu-fra.api.csod.com/rec-job-search/external/jobs"


def _site_html(token: str = "tok-abc-123", api: str = "https://eu-fra.api.csod.com") -> str:
    return (
        f'<html><body>'
        f'<script>csod.context.token = "{token}"; var apiHost = "{api}";</script>'
        f'</body></html>'
    )


def _req(*, req_id: str = "100", title: str = "Engineer", city: str = "Berlin") -> dict:
    return {
        "requisitionId": req_id,
        "displayJobTitle": title,
        "locations": [{"city": city, "state": "BE", "country": "Germany"}],
        "externalDescription": "<p>Build it.</p>",
        "postingEffectiveDate": "2026-04-01T10:00:00Z",
    }


def _search_response(reqs: list[dict], total: int) -> dict:
    return {"data": {"requisitions": reqs, "totalCount": total}, "status": 200}


# --- Registry ---------------------------------------------------------------


def test_registry_resolves_cornerstone() -> None:
    assert ScraperRegistry.get(ATSType.CORNERSTONE) is CornerstoneScraper


# --- Construction -----------------------------------------------------------


def test_default_career_url_built_from_slug() -> None:
    s = CornerstoneScraper("acme")
    assert s.career_url == "https://acme.csod.com/ux/ats/careersite/1/home?c=acme"
    assert s.slug == "acme"
    assert s.company_name == "acme"


def test_full_url_accepted() -> None:
    s = CornerstoneScraper("https://thekids.csod.com/ux/ats/careersite/4/home?c=thekids")
    assert s.career_url.startswith("https://thekids.csod.com")
    assert s.slug == "thekids"
    assert s.site_id == 4


def test_full_url_site_id_used_in_api_request(httpx_mock) -> None:
    career_url = "https://thekids.csod.com/ux/ats/careersite/4/home?c=thekids"
    httpx_mock.add_response(url=career_url, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))

    jobs = CornerstoneScraper(career_url).fetch()

    request = httpx_mock.get_requests(url=API_URL)[0]
    body = json.loads(request.content)
    assert body["careerSiteId"] == 4
    assert body["careerSitePageId"] == 4
    assert str(jobs[0].url) == (
        "https://thekids.csod.com/ux/ats/careersite/4/job/100?c=thekids"
    )


def test_custom_site_id() -> None:
    s = CornerstoneScraper("acme", site_id=4)
    assert "/careersite/4/" in s.career_url


# --- Token extraction -------------------------------------------------------


def test_extracts_token_and_api_host(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    request = httpx_mock.get_requests(url=API_URL)[0]
    assert request.headers["Authorization"] == "Bearer tok-abc-123"
    assert len(jobs) == 1


def test_falls_back_to_na_api_when_host_not_found(httpx_mock) -> None:
    """Most career-site pages embed the regional host; if the regex misses
    it, default to ``na.api.csod.com``."""
    httpx_mock.add_response(
        url=CAREER_URL,
        text='<html><script>csod.context.token = "x";</script></html>',
    )
    httpx_mock.add_response(
        url="https://na.api.csod.com/rec-job-search/external/jobs",
        json=_search_response([_req()], total=1),
    )
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_init_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        CornerstoneScraper("acme").fetch()


def test_missing_token_raises_helpful_error(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text="<html><body>no token</body></html>")
    with pytest.raises(ScraperError, match="couldn't extract JWT token"):
        CornerstoneScraper("acme").fetch()


# --- Culture ----------------------------------------------------------------

FORM_DEFAULT_TITLE = "&lt;&lt;ENTER DISPLAY JOB TITLE&gt;&gt;"


def _site_html_with_culture(culture_id: int, culture_name: str) -> str:
    return (
        "<html><body><script>"
        'csod.context={"corp":"acme","user":-100,'
        f'"cultureID":{culture_id},"cultureName":"{culture_name}",'
        '"endpoints":{"cloud":"https://eu-fra.api.csod.com/","api":"/"},'
        '"token":"tok-abc-123"}'
        "</script></body></html>"
    )


def _asked_cultures(httpx_mock) -> list[tuple[int, str]]:
    bodies = [json.loads(r.content) for r in httpx_mock.get_requests(url=API_URL)]
    return [(body["cultureId"], body["cultureName"]) for body in bodies]


def test_search_asks_for_the_culture_the_site_names(httpx_mock) -> None:
    """A British site keeps its adverts under en-GB; asked in en-US the search
    answers the form's default title for every one of them."""
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(2, "en-GB"))
    httpx_mock.add_response(
        url=API_URL, json=_search_response([_req(title="Teacher")], total=1)
    )

    jobs = CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(2, "en-GB")]
    assert jobs[0].title == "Teacher"


def test_search_asks_for_us_english_when_the_site_names_no_culture(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))

    CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(1, "en-US")]


@pytest.mark.parametrize(
    "context",
    [
        '"cultureID":4,',  # the id without its name
        '"cultureName":"de-DE",',  # the name without its id
        '"cultureID":"x","cultureName":"de-DE",',  # an id that is not a number
    ],
)
def test_half_a_culture_is_not_used(httpx_mock, context: str) -> None:
    html = (
        "<html><script>csod.context={" + context + '"endpoints":{"cloud":'
        '"https://eu-fra.api.csod.com/"},"token":"tok-abc-123"}</script></html>'
    )
    httpx_mock.add_response(url=CAREER_URL, text=html)
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))

    CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(1, "en-US")]


def test_every_page_is_asked_in_the_sites_culture(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    first = [_req(req_id=str(i)) for i in range(25)]
    second = [_req(req_id=str(i)) for i in range(25, 30)]
    httpx_mock.add_response(url=API_URL, json=_search_response(first, total=30))
    httpx_mock.add_response(url=API_URL, json=_search_response(second, total=30))

    jobs = CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(4, "de-DE"), (4, "de-DE")]
    assert [job.ats_id for job in jobs] == [str(i) for i in range(30)]


def test_unfilled_title_is_read_in_us_english(httpx_mock) -> None:
    """Only the requisition without a title in the site's culture changes, and
    it takes the US English advert whole."""
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    german = [
        _req(req_id="1", title=FORM_DEFAULT_TITLE),
        _req(req_id="2", title="Koch"),
    ]
    german[0]["externalDescription"] = "&lt;&lt;INTERNAL JOB DESCRIPTION&gt;&gt;"
    english = [
        _req(req_id="1", title="Cook"),
        _req(req_id="2", title=FORM_DEFAULT_TITLE),
    ]
    english[0]["externalDescription"] = "<p>Cook for the canteen.</p>"
    httpx_mock.add_response(url=API_URL, json=_search_response(german, total=2))
    httpx_mock.add_response(url=API_URL, json=_search_response(english, total=2))

    jobs = CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(4, "de-DE"), (1, "en-US")]
    assert [(job.ats_id, job.title) for job in jobs] == [("1", "Cook"), ("2", "Koch")]
    assert jobs[0].description == "Cook for the canteen."


def test_title_unfilled_in_both_cultures_is_kept_as_the_site_gives_it(
    httpx_mock,
) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    unfilled = [_req(req_id="1", title=FORM_DEFAULT_TITLE)]
    httpx_mock.add_response(url=API_URL, json=_search_response(unfilled, total=1))
    httpx_mock.add_response(
        url=API_URL, json=_search_response([_req(req_id="1", title="null")], total=1)
    )

    jobs = CornerstoneScraper("acme").fetch()

    assert [job.title for job in jobs] == [FORM_DEFAULT_TITLE]


def test_no_second_listing_when_every_title_is_filled(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    httpx_mock.add_response(
        url=API_URL, json=_search_response([_req(title="Koch")], total=1)
    )

    CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(4, "de-DE")]


def test_us_english_site_is_listed_once_even_with_unfilled_titles(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(1, "en-US"))
    httpx_mock.add_response(
        url=API_URL, json=_search_response([_req(title="null")], total=1)
    )

    jobs = CornerstoneScraper("acme").fetch()

    assert _asked_cultures(httpx_mock) == [(1, "en-US")]
    assert [job.title for job in jobs] == ["null"]


def test_failed_us_english_listing_keeps_the_sites_answer(
    monkeypatch, httpx_mock
) -> None:
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 1)
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    german = [_req(req_id="1", title=FORM_DEFAULT_TITLE), _req(req_id="2", title="Koch")]
    httpx_mock.add_response(url=API_URL, json=_search_response(german, total=2))
    httpx_mock.add_response(url=API_URL, status_code=503)

    jobs = CornerstoneScraper("acme").fetch()

    assert [job.title for job in jobs] == [FORM_DEFAULT_TITLE, "Koch"]


@pytest.mark.parametrize(
    ("title", "unfilled"),
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("null", True),
        ("NULL", True),
        ("&lt;", True),
        ("&lt;&lt;ENTER DISPLAY JOB TITLE&gt;&gt; - copy", True),
        ("< Indicar aquí el título de la oferta >", True),
        ("Koch", False),
        ("Null Pointer Hunter", False),
        ("Display job title", False),  # an employer's own wording: left alone
        ("R&D Engineer <Paris>", False),
    ],
)
def test_title_unfilled(title: str | None, unfilled: bool) -> None:
    from ats_scrapers.scrapers.cornerstone import _title_unfilled

    assert _title_unfilled({"displayJobTitle": title}) is unfilled


# --- Posting dates ----------------------------------------------------------


def _dated(*values: object) -> list[dict]:
    return [{"requisitionId": i, "postingEffectiveDate": v} for i, v in enumerate(values)]


def _iso_dates(culture_name: str, *values: object) -> list[object]:
    from ats_scrapers.scrapers.cornerstone import _iso_posting_dates

    rewritten = _iso_posting_dates(_dated(*values), culture_name)
    return [item["postingEffectiveDate"] for item in rewritten]


@pytest.mark.parametrize(
    ("culture_name", "values", "expected"),
    [
        # The culture decides when no date of the listing settles the order.
        ("en-US", ["5/6/2026"], ["2026-05-06"]),
        ("en-GB", ["05/11/2026"], ["2026-11-05"]),
        ("ca-ES", ["11/12/2026"], ["2026-11-12"]),
        # A number above 12 settles it for the whole listing.
        ("en-GB", ["05/11/2026", "13/11/2026"], ["2026-11-05", "2026-11-13"]),
        ("en-US", ["10/21/2026", "11/6/2026"], ["2026-10-21", "2026-11-06"]),
        ("xx-XX", ["11/25/2026", "11/12/2026"], ["2026-11-25", "2026-11-12"]),
        ("en-US", ["24.08.2026", "10.11.2026"], ["2026-08-24", "2026-11-10"]),
        # Punctuation of the cultures seen on live sites.
        ("de-DE", ["24.08.2026"], ["2026-08-24"]),
        ("nl-NL", ["12-11-2026"], ["2026-11-12"]),
        ("sl-SI", ["13. 11. 2026"], ["2026-11-13"]),
        ("hr-HR", ["06. 10. 2026."], ["2026-10-06"]),
        ("bg-BG", ["20.10.2026\u202fг."], ["2026-10-20"]),
        ("pl-PL", ["6.11.2026"], ["2026-11-06"]),
        # Year first: the order is fixed.
        ("fr-CA", ["2026-11-12"], ["2026-11-12"]),
        ("hu-HU", ["2026. 11. 04."], ["2026-11-04"]),
        # Not a date this function reads: left for the ISO parse.
        ("en-US", ["2026-04-01T10:00:00Z", None, "-"], ["2026-04-01T10:00:00Z", None, "-"]),
        # Unreadable dates become None, never a guess.
        ("fr-FR", ["31/02/2026"], [None]),
        ("th-TH", ["13/11/2569"], [None]),
        ("en-US", ["5/6/26"], [None]),
        # Dates that contradict each other: the culture decides, the rest is None.
        ("en-GB", ["13/11/2026", "11/13/2026"], ["2026-11-13", None]),
    ],
)
def test_posting_dates_are_read_in_the_cultures_order(
    culture_name: str, values: list[object], expected: list[object]
) -> None:
    assert _iso_dates(culture_name, *values) == expected


def test_posting_date_of_a_british_site_is_day_first(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(2, "en-GB"))
    req = _req()
    req["postingEffectiveDate"] = "05/11/2026"
    httpx_mock.add_response(url=API_URL, json=_search_response([req], total=1))

    job = CornerstoneScraper("acme").fetch()[0]

    assert job.posted_at is not None
    assert job.posted_at.date().isoformat() == "2026-11-05"


def test_posting_date_of_a_us_site_is_month_first(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    req = _req()
    req["postingEffectiveDate"] = "5/6/2026"
    httpx_mock.add_response(url=API_URL, json=_search_response([req], total=1))

    job = CornerstoneScraper("acme").fetch()[0]

    assert job.posted_at is not None
    assert job.posted_at.date().isoformat() == "2026-05-06"


def test_advert_taken_from_us_english_keeps_its_own_date_order(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html_with_culture(4, "de-DE"))
    german = [_req(req_id="1", title=FORM_DEFAULT_TITLE), _req(req_id="2", title="Koch")]
    english = [_req(req_id="1", title="Cook"), _req(req_id="2", title="Chef")]
    for item in german:
        item["postingEffectiveDate"] = "05.11.2026"
    for item in english:
        item["postingEffectiveDate"] = "11/5/2026"
    httpx_mock.add_response(url=API_URL, json=_search_response(german, total=2))
    httpx_mock.add_response(url=API_URL, json=_search_response(english, total=2))

    jobs = CornerstoneScraper("acme").fetch()

    assert [job.title for job in jobs] == ["Cook", "Koch"]
    assert {job.posted_at.date().isoformat() for job in jobs} == {"2026-11-05"}


# --- Search response parsing ------------------------------------------------


def test_parses_basic_requisition(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    job = jobs[0]
    assert job.ats_id == "100"
    assert job.title == "Engineer"
    assert job.location == "Berlin, BE, Germany"
    assert job.company == "acme"
    assert job.ats_type is ATSType.CORNERSTONE
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_company_name_override_is_used_for_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme", company_name="Acme Corp").fetch()
    assert jobs[0].company == "Acme Corp"


def test_strips_html_from_description(httpx_mock) -> None:
    req = _req()
    req["externalDescription"] = "<p>Hello <b>world</b>.</p>"
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([req], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert jobs[0].description == "Hello world ."


def test_dedupes_requisitions(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL,
        json=_search_response([_req(req_id="A"), _req(req_id="A", title="dup")], total=2),
    )
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_url_uses_career_origin(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req(req_id="X")], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert str(jobs[0].url) == (
        "https://acme.csod.com/ux/ats/careersite/1/job/X?c=acme"
    )


# --- Pagination -------------------------------------------------------------


def test_pagination_via_total_count(httpx_mock) -> None:
    """totalCount=60 with PAGE_SIZE=25 → 3 pages total. Page 1 from first
    init call, pages 2-3 from fan-out."""
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL,
        json=_search_response([_req(req_id=str(i)) for i in range(25)], total=60),
        is_reusable=True,
    )
    jobs = CornerstoneScraper("acme").fetch()
    # Reused mock returns same 25 reqs every page → dedup keeps 25 unique
    assert len({j.ats_id for j in jobs}) == 25


def test_no_pagination_when_total_le_page_size(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


# --- Error handling ---------------------------------------------------------


def test_search_5xx_retries(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch
    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 3)
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, status_code=503)
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    jobs = CornerstoneScraper("acme").fetch()
    assert len(jobs) == 1


def test_429_with_retry_after_honored(monkeypatch, httpx_mock) -> None:
    import ats_scrapers.fetch
    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 3)

    sleeps: list[float] = []
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(
        url=API_URL, status_code=429, headers={"Retry-After": "11"}
    )
    httpx_mock.add_response(url=API_URL, json=_search_response([_req()], total=1))
    CornerstoneScraper("acme").fetch()
    assert 11.0 in sleeps


def test_malformed_json_raises_clean_error(httpx_mock) -> None:
    httpx_mock.add_response(url=CAREER_URL, text=_site_html())
    httpx_mock.add_response(url=API_URL, text="<html>not json</html>")
    with pytest.raises(ScraperError, match="malformed JSON"):
        CornerstoneScraper("acme").fetch()
