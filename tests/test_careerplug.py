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
