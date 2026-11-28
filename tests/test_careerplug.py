from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import CareerPlugScraper, careerplug
from ats_scrapers.scrapers.base import ScraperRegistry

LIST_URL = "https://acme.careerplug.com/jobs"


def _row(job_id: int, title: str, place: str, posted: str | None = None) -> str:
    date = (
        "<div class=\"job-post-date col-md-2\"><div>"
        "<span class='job-row-title hidden-md hidden-lg'>Post Date:</span>"
        f" {posted} </div></div>"
        if posted
        else ""
    )
    return (
        f'<div><a aria-label="{title}" href="/jobs/{job_id}"><div class="row">'
        f'<div class="job-title col-sm-7"><span class="name">{title}</span></div>'
        '<div class="job-location col-md-3"><div>'
        "<span class='job-row-title hidden-md hidden-lg'>Location:</span>"
        f" {place} </div></div>{date}</div></a></div>"
    )


def _page(
    rows: list[str], *, title: str = "Acme", pager: str = "", embedded: list[dict] | None = None,
) -> str:
    data = ""
    if embedded is not None:
        block = {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": index + 1, "item": item}
                for index, item in enumerate(embedded)
            ],
        }
        data = f'<script type="application/ld+json">{json.dumps(block)}</script>'
    table = f'<div id="job_table">{"".join(rows)}</div>' if rows else "<p>Nothing open.</p>"
    return (
        f"<html><head><title>{title}</title>{data}</head><body>"
        f'<div id="job_filters"><form></form></div>{table}{pager}</body></html>'
    )


def _posting(job_id: int, **fields: object) -> dict[str, object]:
    return {
        "@type": "JobPosting",
        "title": "Store Manager",
        "description": "Lead the store.\nHire the team.",
        "datePosted": "2026-11-12T21:40:58+00:00",
        "employmentType": "FULL_TIME",
        "identifier": {"@type": "PropertyValue", "name": "CareerPlug Job ID", "value": str(job_id)},
        "hiringOrganization": {"@type": "Organization", "name": "Acme of Del Mar"},
        "industry": "Retail",
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Del Mar",
                "addressRegion": "CA",
                "postalCode": "92014",
                "addressCountry": "US",
            },
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "unitText": "HOUR",
                "minValue": "30.00",
                "maxValue": "35.00",
            },
        },
        **fields,
    }


PAGER = (
    '<div class="pagination"><em class="current">1</em> '
    '<a rel="next" href="/jobs?page=2">2</a> '
    '<a class="next_page" rel="next" href="/jobs?page=2">next</a></div>'
)
LAST_PAGER = (
    '<div class="pagination"><a href="/jobs?page=1">1</a> <em class="current">2</em> '
    '<span class="next_page disabled">next</span></div>'
)


def test_registry_and_address_resolve_careerplug() -> None:
    assert ScraperRegistry.get(ATSType.CAREERPLUG) is CareerPlugScraper
    assert resolve_careers_url("https://app.careerplug.com/jobs/7") is None


def test_reads_an_employer_site_from_its_list(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL,
        text=_page([_row(7, "Store Manager", "CA-Del Mar-92014")], embedded=[_posting(7)]),
    )

    job = CareerPlugScraper("acme").fetch()[0]

    assert job.global_id == "careerplug:7"
    assert str(job.url) == "https://acme.careerplug.com/jobs/7"
    assert str(job.apply_url) == str(job.url)
    assert job.title == "Store Manager"
    assert job.company == "Acme of Del Mar"
    assert job.location == "Del Mar, CA"
    assert job.country_iso == "US"
    assert job.employment_type == "FULL_TIME"
    assert job.description == "Lead the store.\nHire the team."
    assert job.posted_at == datetime(2026, 11, 12, 21, 40, 58, tzinfo=UTC)
    assert (job.salary_currency, job.salary_period) == ("USD", "HOUR")
    assert (job.salary_min, job.salary_max) == (30.0, 35.0)
    assert job.salary_summary == "30 - 35 per hour"
    assert job.raw == {"industry": "Retail", "postal_code": "92014"}


def test_reads_a_site_that_lists_many_locations(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL,
        text=_page(
            [
                _row(11, "Stylist", "NC-Winston-Salem-27101", "11-18-26"),
                _row(12, "Coach", "ON-Ottawa-K1A 0B1", "11-17-26"),
                _row(13, "Driver", "Anywhere"),
            ],
            title="Acme &amp; Sons Careers",
        ),
    )

    stylist, coach, driver = CareerPlugScraper("acme", include_descriptions=False).fetch()

    assert stylist.company == "Acme & Sons"
    assert stylist.location == "Winston-Salem, NC"
    assert stylist.country_iso == "US"
    assert stylist.raw == {"postal_code": "27101"}
    assert stylist.posted_at == datetime(2026, 11, 18, tzinfo=UTC)
    assert stylist.description is None
    assert (coach.location, coach.country_iso) == ("Ottawa, ON", "CA")
    assert coach.raw == {"postal_code": "K1A 0B1"}
    assert (driver.location, driver.country_iso, driver.posted_at) == ("Anywhere", None, None)


def test_turns_every_page_and_keeps_a_job_once(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL,
        text=_page([_row(1, "Cook", "TX-Austin-78701"), _row(2, "Host", "TX-Austin-78701")], pager=PAGER),
    )
    httpx_mock.add_response(
        url=f"{LIST_URL}?page=2",
        text=_page(
            [_row(2, "Host", "TX-Austin-78701"), _row(3, "Baker", "TX-Austin-78701")],
            pager=LAST_PAGER,
        ),
    )

    jobs = CareerPlugScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3"]


def test_keeps_turning_when_the_pager_names_no_last_page(httpx_mock) -> None:
    only_next = '<div class="pagination"><a class="next_page" href="/jobs?later=1">next</a></div>'
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(1, "Cook", "TX-Austin-78701")], pager=only_next),
    )
    httpx_mock.add_response(
        url=f"{LIST_URL}?page=2", text=_page([_row(2, "Host", "TX-Austin-78701")]),
    )

    jobs = CareerPlugScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_a_list_longer_than_the_page_limit_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(careerplug, "MAX_PAGES", 1)
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(1, "Cook", "TX-Austin-78701")], pager=PAGER),
    )

    with pytest.raises(ScraperError, match="more than 1 pages"):
        CareerPlugScraper("acme", include_descriptions=False).fetch()


def test_a_list_still_turning_at_the_page_limit_is_an_error(httpx_mock, monkeypatch) -> None:
    monkeypatch.setattr(careerplug, "MAX_PAGES", 2)
    only_next = '<div class="pagination"><a class="next_page" href="/jobs?later=1">next</a></div>'
    for url, job_id in ((LIST_URL, 1), (f"{LIST_URL}?page=2", 2)):
        httpx_mock.add_response(
            url=url, text=_page([_row(job_id, "Cook", "TX-Austin-78701")], pager=only_next),
        )

    with pytest.raises(ScraperError, match="more than 2 pages"):
        CareerPlugScraper("acme", include_descriptions=False).fetch()


def test_reads_a_missing_description_from_the_posting_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL,
        text=_page(
            [_row(7, "Store Manager", "CA-Del Mar-92014"), _row(8, "Cashier", "BC-Surrey-V3T 1V8")],
            title="Acme Careers",
            embedded=[_posting(7)],
        ),
    )
    httpx_mock.add_response(
        url="https://acme.careerplug.com/jobs/8",
        status_code=302,
        headers={"location": "https://acme.careerplug.com/jobs/8/apps/new"},
    )
    escaped = "&lt;p&gt;Greet guests.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Run the till&lt;/li&gt;&lt;/ul&gt;"
    detail = _posting(
        8,
        description=escaped,
        employmentType="PART_TIME",
        hiringOrganization={"@type": "Organization", "name": "Acme Surrey"},
        jobLocation={"@type": "Place", "address": {"addressCountry": "CA", "postalCode": "V3T 1V8"}},
        baseSalary={"currency": "CAD", "value": {"unitText": "HOUR", "value": "18.50"}},
    )
    httpx_mock.add_response(
        url="https://acme.careerplug.com/jobs/8/apps/new",
        text=f'<html><script type="application/ld+json">{json.dumps(detail)}</script></html>',
    )

    manager, cashier = CareerPlugScraper("acme").fetch()

    assert manager.description == "Lead the store.\nHire the team."
    assert cashier.description == "Greet guests.\nRun the till"
    assert cashier.company == "Acme Surrey"
    assert cashier.country_iso == "CA"
    assert cashier.employment_type == "PART_TIME"
    assert (cashier.salary_currency, cashier.salary_min, cashier.salary_max) == ("CAD", 18.5, 18.5)
    assert cashier.salary_summary == "18.5 per hour"
    assert cashier.raw == {"postal_code": "V3T 1V8", "industry": "Retail"}


def test_without_descriptions_no_posting_page_is_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL,
        text=_page(
            [_row(7, "Store Manager", "CA-Del Mar-92014"), _row(8, "Cashier", "CA-Del Mar-92014")],
            embedded=[_posting(7)],
        ),
    )

    manager, cashier = CareerPlugScraper("acme", include_descriptions=False).fetch()

    assert manager.description == "Lead the store.\nHire the team."
    assert cashier.description is None


def test_keeps_a_job_whose_posting_page_shows_something_else(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(8, "Cashier", "TX-Austin-78701")], title="Acme Careers"),
    )
    other = json.dumps(_posting(9))
    httpx_mock.add_response(
        url="https://acme.careerplug.com/jobs/8",
        text=f'<html><script type="application/ld+json">{other}</script></html>',
    )

    job = CareerPlugScraper("acme").fetch()[0]

    assert (job.ats_id, job.company, job.description) == ("8", "Acme", None)


# A posting page without schema.org data, as served: the posting's own text,
# then the account's blurb and disclaimer, then the application form.
_PAGE_WITHOUT_POSTING_DATA = """<html><head><title>Cashier | Acme Overland Park</title>
<link rel="canonical" href="https://acme-overland-park.careerplug.com/jobs/{canonical}/apps/new">
</head><body><div class="apply-page-main-content">
<div class="job-description-container">
  <div class="benefits"><h3>Benefits:</h3><ul><li>Free meals</li></ul></div>
  <div class="trix-content"><div>REPORTS TO: General Manager<br><br></div>
  <div><strong>Job Requirements:</strong></div><ul><li>Able to work fast.</li></ul></div>
  <div class="job-compensation">Compensation: $12.00 per hour</div>
  <br><br><br><div class="account_description"><p>About Acme.</p></div>
  <br><div class="disclaimer-v2"><p>Franchise disclaimer.</p></div>
</div>
<div class="app-form"><form class="single_submit formtastic app" id="new_app" action="{action}"
 accept-charset="UTF-8" method="post"></form></div>
</div></body></html>"""


def _posting_page(*, action: str = "/jobs/8/apps", canonical: str = "8") -> str:
    return _PAGE_WITHOUT_POSTING_DATA.format(action=action, canonical=canonical)


@pytest.mark.parametrize(
    ("action", "canonical"),
    [("/jobs/8/apps", "8"), ("/jobs/8/apps", "9"), ("/jobs/9/apps", "8")],
)
def test_reads_the_text_of_a_posting_page_without_posting_data(
    httpx_mock, action: str, canonical: str,
) -> None:
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(8, "Cashier", "KS-Overland Park-66210")], title="Acme Careers"),
    )
    httpx_mock.add_response(
        url="https://acme.careerplug.com/jobs/8",
        text=_posting_page(action=action, canonical=canonical),
    )

    job = CareerPlugScraper("acme").fetch()[0]

    assert job.description == "REPORTS TO: General Manager\nJob Requirements:\nAble to work fast."
    assert job.location == "Overland Park, KS"


def test_reads_the_text_of_an_older_posting_page(httpx_mock) -> None:
    page = """<html><body class="microsite apps microsite-v1"><div id="main-apply-page-wrapper">
    <div class="col-md-7 col-md-height"><div id="job-description-wrapper" class="section">
    <div class="trix-content"><p>Admin assistant.<br><br>REQUIREMENTS:<br>Good math skills</p></div>
    <div class="disclaimer"><p>Independently owned.</p></div>
    <div class="company_info"><div class="account_description"><p>About Acme.</p></div></div>
    </div></div>
    <div class="col-md-4"><form class="single_submit formtastic app" action="/jobs/8/apps">
    <div class="trix-content"><p>Question for applicants.</p></div></form></div>
    </div></body></html>"""
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(8, "Assistant", "TX-Prosper-75078")], title="Acme Careers"),
    )
    httpx_mock.add_response(url="https://acme.careerplug.com/jobs/8", text=page)

    job = CareerPlugScraper("acme").fetch()[0]

    assert job.description == "Admin assistant.\nREQUIREMENTS:\nGood math skills"


def test_a_page_without_posting_data_for_another_posting_gives_nothing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL, text=_page([_row(8, "Cashier", "KS-Overland Park-66210")], title="Acme Careers"),
    )
    httpx_mock.add_response(
        url="https://acme.careerplug.com/jobs/8",
        text=_posting_page(action="/jobs/9/apps", canonical="9"),
    )

    job = CareerPlugScraper("acme").fetch()[0]

    assert job.description is None


def test_reads_place_and_date_beside_a_link_that_holds_only_the_title(httpx_mock) -> None:
    rows = (
        '<div><div class="row"><div class="job-title col-md-7">'
        '<a aria-label="Shift Leader in Mansfield, TX" href="/jobs/3701474">'
        '<span class="name">Shift Leader</span></a></div>'
        '<div class="job-location col-md-3">TX-Mansfield-76063</div>'
        '<div class="job-post-date col-md-2">11-20-26</div></div></div>'
    )
    httpx_mock.add_response(url=LIST_URL, text=_page([rows], title="Acme Careers"))

    job = CareerPlugScraper("acme", include_descriptions=False).fetch()[0]

    assert (job.ats_id, job.title) == ("3701474", "Shift Leader")
    assert (job.location, job.country_iso) == ("Mansfield, TX", "US")
    assert job.raw == {"postal_code": "76063"}
    assert job.posted_at == datetime(2026, 11, 20, tzinfo=UTC)


def _title_only_row(job_id: int, title: str, place: str, posted: str) -> str:
    return (
        f'<div class="row"><div class="job-title col-md-7">'
        f'<a href="/jobs/{job_id}"><span class="name">{title}</span></a></div>'
        f'<div class="job-location col-md-3">{place}</div>'
        f'<div class="job-post-date col-md-2">{posted}</div></div>'
    )


@pytest.mark.parametrize("title_only", [False, True])
def test_rows_inside_an_outer_row_keep_their_own_place(httpx_mock, title_only: bool) -> None:
    if title_only:
        rows = [
            _title_only_row(1, "Cook", "TX-Austin-78701", "11-20-26"),
            _title_only_row(2, "Host", "OR-Eugene-97401", "11-18-26"),
        ]
    else:
        rows = [
            _row(1, "Cook", "TX-Austin-78701", "11-20-26"),
            _row(2, "Host", "OR-Eugene-97401", "11-18-26"),
        ]
    httpx_mock.add_response(url=LIST_URL, text=_page([f'<div class="row">{"".join(rows)}</div>']))

    jobs = CareerPlugScraper("acme", include_descriptions=False).fetch()

    assert [job.location for job in jobs] == ["Austin, TX", "Eugene, OR"]
    assert [job.posted_at.day for job in jobs] == [20, 18]


def test_a_site_with_nothing_open_is_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text=_page([]))

    assert CareerPlugScraper("acme").fetch() == []


def test_an_unknown_account_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=LIST_URL, status_code=302, headers={"location": "https://app.careerplug.com/"},
    )

    with pytest.raises(CompanyNotFoundError):
        CareerPlugScraper("acme").fetch()


def test_a_page_without_a_job_list_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=LIST_URL, text="<html><body>Sign in</body></html>")

    with pytest.raises(ScraperError, match="unrecognized"):
        CareerPlugScraper("acme").fetch()


@pytest.mark.parametrize("slug", ["app", "www", "acme.example", ""])
def test_rejects_what_is_not_an_account(slug: str) -> None:
    with pytest.raises(ScraperError):
        CareerPlugScraper(slug)
