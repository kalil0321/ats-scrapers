"""Tests for the Ashby scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import AshbyScraper, ScraperRegistry

API = "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"


# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture
# in conftest.py — the shared fetch layer replaced per-scraper retry
# constants.


HOSTED = "https://jobs.ashbyhq.com/acme"


def _hosted_pages(httpx_mock, status_code: int = 200, board: str = HOSTED) -> None:
    """The HEAD request that tells whether the board's hosted pages are open."""
    httpx_mock.add_response(method="HEAD", url=board, status_code=status_code)


def _job(jid: str = "j1", title: str = "SWE", location: str = "Remote") -> dict:
    return {
        "id": jid,
        "title": title,
        "location": location,
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{jid}",
        "publishedAt": "2026-04-15T08:00:00.000Z",
    }


def test_registry_resolves_ashby() -> None:
    assert ScraperRegistry.get(ATSType.ASHBY) is AshbyScraper


def test_parses_basic_job(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    jobs = AshbyScraper("acme").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "SWE"
    assert job.company == "acme"
    assert job.ats_type is ATSType.ASHBY


def test_returns_empty_list(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json={"jobs": []})
    assert AshbyScraper("acme").fetch() == []


def test_404_company_not_found(httpx_mock) -> None:
    """A board that is gone answers 404 on the API and on the embed view."""
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url="https://jobs.ashbyhq.com/acme?embed=js", status_code=404)
    with pytest.raises(CompanyNotFoundError):
        AshbyScraper("acme").fetch()


def test_compensation_summary_passthrough(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    """The summary string is always preserved when the compensation block
    carries one — structured min/max may or may not be set depending on
    whether ``summaryComponents`` is in the exact shape the parser
    expects, so we only assert on the more permissive summary field."""
    j = _job()
    j["compensation"] = {"compensationTierSummary": "$120k - $180k"}
    httpx_mock.add_response(url=API, json={"jobs": [j]})
    job = AshbyScraper("acme").fetch()[0]
    assert job.salary_summary == "$120k - $180k"


def test_5xx_retries(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=503)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    assert len(AshbyScraper("acme").fetch()) == 1


# --- hosted board: the API is off for this employer, the page is public -----

BOARD = "https://jobs.ashbyhq.com/acme"
EMBED_BOARD = BOARD + "?embed=js"


def _page(data: dict) -> str:
    import json

    return (
        "<html><head><script>window.__appData = "
        + json.dumps(data)
        + ';window.other = {"a": 1};</script></head><body></body></html>'
    )


def _board(*postings: dict) -> str:
    return _page({"organization": {"name": "Acme"}, "jobBoard": {"jobPostings": list(postings)}})


def _posting(jid: str = "p1", title: str = "Account Executive") -> dict:
    return {
        "id": jid,
        "title": title,
        "locationName": "San Francisco, CA",
        "workplaceType": "Hybrid",
        "employmentType": "FullTime",
        "secondaryLocations": [{"locationName": "New York, NY"}],
        "compensationTierSummary": None,
    }


def _detail(jid: str = "p1", title: str = "Account Executive") -> str:
    return _page(
        {
            "posting": {
                "id": jid,
                "title": title,
                "departmentName": "Go To Market",
                "teamNames": ["Go To Market", "Sales"],
                "locationName": "San Francisco, CA",
                "secondaryLocationNames": ["New York, NY", "Los Angeles, CA"],
                "workplaceType": "Hybrid",
                "employmentType": "FullTime",
                "descriptionHtml": "<p>Sell things.</p>",
                "compensationTierSummary": "$120K – $135K • Offers Equity",
                "scrapeableCompensationSalarySummary": "$120K - $135K",
            }
        }
    )


def test_api_404_reads_the_hosted_board(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board(_posting("p1"), _posting("p2", "Designer")))
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_detail("p1"))
    httpx_mock.add_response(url=f"{BOARD}/p2?embed=js", text=_detail("p2", "Designer"))

    jobs = AshbyScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["p1", "p2"]
    job = jobs[0]
    assert job.title == "Account Executive"
    assert str(job.url) == f"{BOARD}/p1"
    assert str(job.apply_url) == f"{BOARD}/p1/application"
    assert job.location == "San Francisco, CA"
    assert job.description == "<p>Sell things.</p>"
    assert job.department == "Go To Market"
    assert job.team == "Sales"
    assert job.employment_type == "FULL_TIME"
    assert job.is_remote is None
    assert job.salary_summary == "$120K – $135K • Offers Equity"
    assert job.posted_at is None  # the page carries no publication date
    assert job.raw["source"] == "hosted_board"
    assert job.raw["secondary_locations"] == ["New York, NY", "Los Angeles, CA"]
    assert jobs[1].title == "Designer"


def test_hosted_board_without_descriptions_reads_one_posting_page(httpx_mock) -> None:
    """Descriptions off: the listing, and the first posting page to make sure
    the postings can be opened. A request to a second page would fail this test."""
    _hosted_pages(httpx_mock)
    teams = [
        {"id": "t-dept", "name": "Go To Market", "parentTeamId": None},
        {"id": "t-sales", "name": "Sales", "parentTeamId": "t-dept"},
    ]
    first = {**_posting("p1"), "teamId": "t-sales", "compensationTierSummary": "$120K – $135K"}
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(
        url=EMBED_BOARD,
        text=_page({"jobBoard": {"teams": teams, "jobPostings": [first, _posting("p2")]}}),
    )
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_detail("p1"))

    job, other = AshbyScraper("acme", include_descriptions=False).fetch()

    assert job.description is None
    assert job.department == "Go To Market"
    assert job.team == "Sales"
    assert job.location == "San Francisco, CA"
    assert job.salary_summary == "$120K – $135K"
    assert job.raw["secondary_locations"] == ["New York, NY"]
    assert other.ats_id == "p2"


def test_listing_whose_postings_cannot_be_opened_is_empty(httpx_mock) -> None:
    """Personio's embed view listed 77 postings on 14 Nov 2026 and every
    posting page answered 404. A job nobody can open is not published."""
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(
        url=EMBED_BOARD,
        text=_board(*(_posting(f"p{number}") for number in range(1, 6))),
    )
    for number in (1, 2, 3):  # three in a row, then no more requests
        httpx_mock.add_response(url=f"{BOARD}/p{number}?embed=js", status_code=404)

    assert AshbyScraper("acme", include_descriptions=False).fetch() == []


def test_one_closed_posting_does_not_empty_the_listing(httpx_mock) -> None:
    """The first posting closed after the listing was read: it is dropped,
    the others stay."""
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(
        url=EMBED_BOARD, text=_board(_posting("p1"), _posting("p2"), _posting("p3"))
    )
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", status_code=404)
    httpx_mock.add_response(url=f"{BOARD}/p2?embed=js", text=_detail("p2"))

    jobs = AshbyScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["p2", "p3"]


def test_a_failed_posting_probe_keeps_the_listing(httpx_mock) -> None:
    """A server error on the probed posting says nothing about the board:
    the listing just read is returned."""
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board(_posting("p1"), _posting("p2")))
    for _ in range(3):
        httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", status_code=503)

    jobs = AshbyScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["p1", "p2"]


# --- hosted pages closed: the jobs show on the employer's own site ----------


def test_closed_hosted_pages_link_to_the_embed_view(httpx_mock) -> None:
    """Cursor: the API lists the jobs, ``jobs.ashbyhq.com/cursor`` answers
    404, and so does every ``jobUrl`` the API returns. The embed view of the
    same pages opens the posting."""
    job = {**_job(), "applyUrl": f"{HOSTED}/j1/application"}
    httpx_mock.add_response(url=API, json={"jobs": [job]})
    _hosted_pages(httpx_mock, status_code=404)
    httpx_mock.add_response(
        url=EMBED_BOARD,
        text=_page({"organization": {"customJobsPageUrl": "https://acme.com/careers"}}),
    )

    (parsed,) = AshbyScraper("acme").fetch()

    assert str(parsed.url) == f"{HOSTED}/j1?embed=js"
    assert str(parsed.apply_url) == f"{HOSTED}/j1/application?embed=js"
    assert parsed.raw["hosted_pages"] == "closed"
    assert parsed.raw["careers_page"] == "https://acme.com/careers"


def test_closed_hosted_pages_without_a_readable_embed_view(httpx_mock) -> None:
    """The links are fixed even when the employer's careers page stays unknown."""
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    _hosted_pages(httpx_mock, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text="<html><body>Jobs</body></html>")

    (parsed,) = AshbyScraper("acme").fetch()

    assert str(parsed.url) == f"{HOSTED}/j1?embed=js"
    assert parsed.raw["hosted_pages"] == "closed"
    assert "careers_page" not in parsed.raw


def test_a_failed_link_check_leaves_the_links_alone(httpx_mock) -> None:
    """Only a 404 says the hosted pages are closed. A check that fails
    otherwise must not change, or cost, the employer's jobs."""
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    for _ in range(3):
        _hosted_pages(httpx_mock, status_code=503)

    (parsed,) = AshbyScraper("acme").fetch()

    assert str(parsed.url) == f"{HOSTED}/j1"
    assert "hosted_pages" not in (parsed.raw or {})


def test_an_empty_board_is_not_checked(httpx_mock) -> None:
    """No job, no link to check: a HEAD request here would fail this test."""
    httpx_mock.add_response(url=API, json={"jobs": []})

    assert AshbyScraper("acme").fetch() == []


def test_api_jobs_never_touch_the_hosted_board(httpx_mock) -> None:
    """Only the API is called when it answers; an unexpected request to the
    page would fail this test."""
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})

    (job,) = AshbyScraper("acme").fetch()

    assert "source" not in (job.raw or {})


def test_hosted_page_without_a_board_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_page({"organization": None, "jobBoard": None}))

    with pytest.raises(CompanyNotFoundError):
        AshbyScraper("acme").fetch()


def test_hosted_board_without_openings_is_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board())

    assert AshbyScraper("acme").fetch() == []


def test_hosted_page_without_app_data_fails_closed(httpx_mock) -> None:
    """A page we cannot read is an error, not an empty board: the previous
    run's jobs must not be dropped because Ashby changed its markup."""
    from ats_scrapers.exceptions import ScraperError

    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text="<html><body>Jobs</body></html>")

    with pytest.raises(ScraperError) as excinfo:
        AshbyScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_posting_closed_between_listing_and_detail_is_skipped(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board(_posting("p1"), _posting("p2")))
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", status_code=404)
    httpx_mock.add_response(url=f"{BOARD}/p2?embed=js", text=_detail("p2"))

    assert [job.ats_id for job in AshbyScraper("acme").fetch()] == ["p2"]


def test_unreadable_posting_page_keeps_the_job_without_description(httpx_mock) -> None:
    """The listing says the opening exists. A posting page we cannot read
    costs that job its description, not the employer all of its jobs."""
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board(_posting("p1"), _posting("p2")))
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_page({"posting": None}))
    httpx_mock.add_response(url=f"{BOARD}/p2?embed=js", text="<html><body>Jobs</body></html>")

    jobs = AshbyScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["p1", "p2"]
    assert [job.description for job in jobs] == [None, None]
    assert jobs[0].location == "San Francisco, CA"


@pytest.mark.parametrize(
    "board",
    [
        {"teams": []},  # no jobPostings field
        {"jobPostings": None},
        {"jobPostings": {"p1": {}}},
        {"jobPostings": [{"title": "No id"}]},
        {"jobPostings": [{"id": "p1"}]},
        {"jobPostings": ["p1"]},
    ],
)
def test_unreadable_listing_fails_closed(httpx_mock, board) -> None:
    """A listing we cannot read is an error, never an empty or partial board."""
    from ats_scrapers.exceptions import ScraperError

    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_page({"jobBoard": board}))

    with pytest.raises(ScraperError) as excinfo:
        AshbyScraper("acme").fetch()
    assert not isinstance(excinfo.value, CompanyNotFoundError)


def test_get_description_reads_the_posting_page_of_a_hosted_job(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, status_code=404)
    httpx_mock.add_response(url=EMBED_BOARD, text=_board(_posting("p1")))
    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_detail("p1"))
    scraper = AshbyScraper("acme", include_descriptions=False)
    (job,) = scraper.fetch()
    assert job.description is None

    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_detail("p1"))
    assert scraper.get_description(job) == "<p>Sell things.</p>"

    httpx_mock.add_response(url=f"{BOARD}/p1?embed=js", text=_detail("p1"))
    (enriched,) = scraper.enrich_descriptions([job])
    assert enriched.description == "<p>Sell things.</p>"


def test_get_description_of_an_api_job_makes_no_request(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    httpx_mock.add_response(url=API, json={"jobs": [_job()]})
    scraper = AshbyScraper("acme")
    (job,) = scraper.fetch()

    assert scraper.get_description(job) == job.description


def test_hosted_board_of_a_slug_with_a_space(httpx_mock) -> None:
    """Catalogue slugs are stored percent-encoded (``marathon%20fusion``)."""
    slug = "marathon%20fusion"
    httpx_mock.add_response(
        url=f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        status_code=404,
    )
    httpx_mock.add_response(
        url=f"https://jobs.ashbyhq.com/{slug}?embed=js", text=_board(_posting("p1"))
    )
    httpx_mock.add_response(
        url=f"https://jobs.ashbyhq.com/{slug}/p1?embed=js", text=_detail("p1")
    )
    _hosted_pages(httpx_mock, board=f"https://jobs.ashbyhq.com/{slug}")

    (job,) = AshbyScraper(slug).fetch()

    assert str(job.url) == f"https://jobs.ashbyhq.com/{slug}/p1"


def test_each_compensation_component_keeps_its_own_tier_and_unit(httpx_mock) -> None:
    _hosted_pages(httpx_mock)
    salary = {"compensationType": "Salary", "minValue": 191000, "maxValue": 214500,
              "currencyCode": "USD", "interval": "1 YEAR",
              "summary": "On Target Earnings $191K – $214.5K"}
    tiers = [
        {"title": "US Remote", "components": [
            salary,
            {"compensationType": "Commission", "summary": "Offers Commission",
             "currencyCode": "USD", "interval": "1 YEAR"},
            {"compensationType": "EquityCashValue", "summary": "Offers Equity",
             "currencyCode": "USD", "interval": "1 YEAR"},
        ]},
        {"title": "Tier 2", "additionalInformation": "Only in Ontario", "components": [
            {"compensationType": "EquityPercentage", "minValue": 0, "maxValue": 1.75,
             "interval": "NONE"},
            {"compensationType": "Bonus", "minValue": 0, "maxValue": 4500,
             "interval": "6 MONTH", "currencyCode": "CAD"},
        ]},
    ]
    item = {**_job(), "compensation": {"compensationTiers": tiers, "summaryComponents": [salary]}}
    httpx_mock.add_response(url=API, json={"jobs": [item]})
    job, = AshbyScraper("acme").fetch()
    ote, commission, cash_equity, percent_equity, bonus = job.compensation_components
    assert (ote.kind, ote.source_label, ote.tier, ote.summary) == (
        "other", "Salary", "US Remote", salary["summary"],
    )
    assert (ote.minimum, ote.maximum, ote.currency, ote.period) == (191000, 214500, "USD", "YEAR")
    assert (commission.kind, commission.summary, commission.minimum, commission.maximum) == (
        "commission", "Offers Commission", None, None,
    )
    assert (cash_equity.kind, cash_equity.unit, cash_equity.maximum) == ("equity", "money", None)
    assert (percent_equity.unit, percent_equity.minimum, percent_equity.maximum,
            percent_equity.period) == ("percent", 0, 1.75, None)
    assert bonus.tier == percent_equity.tier == "Tier 2 — Only in Ontario"
    assert (bonus.kind, bonus.currency, bonus.period, bonus.minimum) == ("bonus", "CAD", None, 0)
    assert job.raw["compensation_tiers"] == tiers
    assert (job.salary_min, job.salary_max, job.salary_period) == (191000, 214500, "YEAR")
    assert len(httpx_mock.get_requests()) == 2


def test_summary_components_used_only_when_tiers_have_no_component_rows() -> None:
    item = {**_job(), "compensation": {"compensationTiers": [], "summaryComponents": [
        {"compensationType": "Salary", "interval": "2 WEEK", "currencyCode": "USD", "minValue": 0},
        {"compensationType": "NewType", "summary": "Employer-defined award"},
    ]}}
    job = AshbyScraper("acme")._parse_job(item)
    salary, unknown = job.compensation_components
    assert (salary.kind, salary.minimum, salary.maximum, salary.period) == ("base_salary", 0, None, None)
    assert job.raw["compensation_summary_components"][0]["interval"] == "2 WEEK"
    assert (unknown.kind, unknown.source_label, unknown.unit) == ("other", "NewType", None)
    assert job.salary_min is None and job.salary_period is None
    assert AshbyScraper("acme")._parse_job(_job()).compensation_components is None


@pytest.mark.parametrize(("record", "kind", "unit", "currency"), [
    ({"compensationType": "Bonus", "minValue": 10}, "bonus", None, None),
    ({"compensationType": "Bonus", "summary": "10% target", "currencyCode": "USD"}, "bonus", "percent", None),
    ({"compensationType": "Bonus", "summary": "$500 signing bonus"}, "bonus", "money", None),
    ({"compensationType": "EquityShares", "currencyCode": "USD"}, "equity", "shares", None),
    ({"compensationType": "EquityPercentage", "currencyCode": "USD"}, "equity", "percent", None),
    ({"compensationType": "Salary", "summary": "Total compensation $230K", "currencyCode": "USD"},
     "other", "money", "USD"),
    ({"compensationType": "NewType", "currencyCode": "USD"}, "other", None, None),
])
def test_component_kind_and_unit_follow_source(record, kind, unit, currency):
    item = {**_job(), "compensation": {"summaryComponents": [record]}}
    component, = AshbyScraper("acme")._parse_job(item).compensation_components
    assert (component.kind, component.unit, component.currency) == (kind, unit, currency)
    assert component.summary == record.get("summary")


def test_base_salary_does_not_inherit_other_components_ote_label():
    item = {**_job(), "compensation": {"compensationTiers": [{
        "tierSummary": "Base salary $80K • OTE $120K", "components": [
            {"compensationType": "Salary", "minValue": 80000, "currencyCode": "USD"},
            {"compensationType": "Salary", "minValue": 120000, "summary": "OTE $120K"},
        ],
    }]}}
    base, ote = AshbyScraper("acme")._parse_job(item).compensation_components
    assert (base.kind, base.minimum, ote.kind, ote.minimum) == ("base_salary", 80000, "other", 120000)


def test_location_addresses_belong_to_each_site() -> None:
    item = {
        **_job(location="Central office, Riverside"),
        "workplaceType": "Hybrid", "isRemote": False,
        "address": {"postalAddress": {"addressLocality": "Riverside", "addressRegion": "CA",
                                      "addressCountry": "USA"}},
        "secondaryLocations": [
            {"location": "Berlin", "address": {"addressLocality": "Berlin", "addressCountry": "de"}},
            {"location": "Europe", "address": {"postalAddress": {"addressCountry": "European Union"}}},
        ],
    }
    job = AshbyScraper("acme")._parse_job(item)
    assert job.workplace_type == "hybrid" and job.is_remote is False
    assert [(p.label, p.city, p.region, p.country_iso, p.is_primary) for p in job.locations] == [
        ("Central office, Riverside", "Riverside", "CA", "US", True),
        ("Berlin", "Berlin", None, "DE", False),
        ("Europe", None, None, None, False),
    ]
    bare = AshbyScraper("acme")._parse_job({**_job(), "isRemote": True})
    assert bare.workplace_type is None and bare.locations is None


@pytest.mark.parametrize("country", ["EU", "ZZ", "ß"])
def test_non_country_codes_do_not_turn_into_location_countries(country) -> None:
    item = {**_job(location="European offices"), "address": {"postalAddress": {"addressCountry": country}}}
    [place] = AshbyScraper("acme")._parse_job(item).locations
    assert place.label == "European offices" and place.country_iso is None
