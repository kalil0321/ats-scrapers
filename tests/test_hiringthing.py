from __future__ import annotations

import html
import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import HiringThingScraper
from ats_scrapers.scrapers.base import ScraperRegistry

BOARD = "https://acme.hiringthing.com"


def _props(value: dict) -> str:
    return html.escape(json.dumps(value), quote=True)


def _card(job_id: int, title: str, place: str, salary: dict | None = None) -> str:
    pay = (
        '<div class="job-salary"><div data-react-class="HiringThing.Components.JobSalary" '
        f'data-react-props="{_props(salary)}"></div></div>'
        if salary is not None
        else ""
    )
    path = f"/job/{job_id}/{title.lower().replace(' ', '-')}"
    return (
        f'<div class="job-container" data-job-id="{job_id}">'
        f'<a class="mobile-apply-link" href="{path}"><h2>{title}</h2></a>'
        f'<div class="job-location"> {place} </div>{pay}'
        f'<a href="{path}"><div class="job-content-button">Learn more</div></a>'
        '<div class="job-description">The start of the text…</div></div>'
    )


def _board(cards: list[str], *, remote: list[int] | None = None, name: str = "Acme &amp; Co") -> str:
    filters = _props({"titles": [], "remotePositions": remote or [], "showAll": True})
    return (
        f'<html><head><title>Acme Career Opportunities</title>'
        f'<meta property="og:title" content="{name}" /></head><body>'
        f'<div data-react-class="HiringThing.Components.JobFiltersContainer" '
        f'data-react-props="{filters}"></div>'
        f'<div class="jobs-list-container">{"".join(cards)}</div></body></html>'
    )


def _job_page(**fields: object) -> str:
    posting = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": "Welder",
        "datePosted": "2026-11-13T08:37:26-08:00",
        "description": "<p><strong>About us</strong></p><p>We weld.</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Acme & Co"},
        "jobLocation": (
            "{'@type': 'Place', 'address': {'@type': 'PostalAddress', 'streetAddress': '', "
            "'addressLocality': 'Grover Beach', 'addressRegion': 'CA', "
            "'postalCode': '93433', 'addressCountry': 'US'}}"
        ),
        **fields,
    }
    return f'<html><script type="application/ld+json">{json.dumps(posting)}</script></html>'


def test_registry_and_address_resolve_hiringthing() -> None:
    assert ScraperRegistry.get(ATSType.HIRINGTHING) is HiringThingScraper
    assert resolve_careers_url("https://www.hiringthing.com/pricing") is None


def test_reads_a_board_from_its_front_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{BOARD}/",
        text=_board(
            [
                _card(
                    7, "Welder", "Grover Beach, CA",
                    {
                        "minSalary": {"amount": "150000.00", "currency": "USD"},
                        "maxSalary": {"amount": "185000.00", "currency": "USD"},
                        "payFrequency": "annually",
                    },
                ),
                _card(8, "Planner", "Remote - Petaluma, CA", {"minSalary": {}, "maxSalary": {}}),
                _card(
                    9, "Porter", "Boulder, CO",
                    {"minSalary": {"amount": "750.00", "currency": "USD"}, "maxSalary": {},
                     "payFrequency": "monthly"},
                ),
            ],
            remote=[8],
        ),
    )

    welder, planner, porter = HiringThingScraper("acme", include_descriptions=False).fetch()

    assert welder.global_id == "hiringthing:7"
    assert str(welder.url) == f"{BOARD}/job/7/welder"
    assert str(welder.apply_url) == str(welder.url)
    assert welder.company == "Acme & Co"
    assert welder.location == "Grover Beach, CA"
    assert welder.is_remote is None
    assert (welder.salary_min, welder.salary_max) == (150000.0, 185000.0)
    assert (welder.salary_currency, welder.salary_period) == ("USD", "YEAR")
    assert welder.salary_summary == "150000 - 185000 per year"
    assert welder.description is None
    assert planner.is_remote is True
    assert (planner.salary_min, planner.salary_summary) == (None, None)
    assert (porter.salary_min, porter.salary_max) == (750.0, None)
    assert porter.salary_summary == "750 per month"


def test_reads_the_description_from_the_job_page(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{BOARD}/", text=_board([_card(7, "Welder", "Grover Beach, CA")]))
    httpx_mock.add_response(url=f"{BOARD}/job/7/welder", text=_job_page())

    job = HiringThingScraper("acme").fetch()[0]

    assert job.description == "About us\nWe weld."
    assert job.posted_at == datetime(2026, 11, 13, 16, 37, 26, tzinfo=UTC)
    assert job.country_iso == "US"
    assert job.raw == {"postal_code": "93433"}


def test_a_job_page_without_a_readable_address_still_gives_the_text(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{BOARD}/", text=_board([_card(7, "Welder", "Grover Beach, CA")]))
    httpx_mock.add_response(
        url=f"{BOARD}/job/7/welder", text=_job_page(jobLocation="__import__('os').getcwd()"),
    )

    job = HiringThingScraper("acme").fetch()[0]

    assert (job.description, job.country_iso, job.raw) == ("About us\nWe weld.", None, None)


def test_a_job_that_closed_meanwhile_keeps_its_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{BOARD}/", text=_board([_card(7, "Welder", "Grover Beach, CA")]))
    httpx_mock.add_response(
        url=f"{BOARD}/job/7/welder", status_code=302, headers={"location": f"{BOARD}/"},
    )

    job = HiringThingScraper("acme").fetch()[0]

    assert (job.ats_id, job.description) == ("7", None)


def test_a_board_on_a_partner_domain_is_named_by_its_address(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://acme.partner-hire.com/", text=_board([_card(7, "Welder", "Reno, NV")]),
    )

    scraper = HiringThingScraper("https://Acme.partner-hire.com/jobs?x=1", include_descriptions=False)
    job = scraper.fetch()[0]

    assert scraper.company_slug == "https://acme.partner-hire.com"
    assert str(job.url) == "https://acme.partner-hire.com/job/7/welder"


def test_a_card_whose_link_names_another_job_is_left_out(httpx_mock) -> None:
    stray = _card(7, "Welder", "Reno, NV").replace('data-job-id="7"', 'data-job-id="70"')
    httpx_mock.add_response(url=f"{BOARD}/", text=_board([stray, _card(8, "Planner", "Reno, NV")]))

    jobs = HiringThingScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["8"]


def test_a_board_with_nothing_open_is_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{BOARD}/", text=_board([]))

    assert HiringThingScraper("acme").fetch() == []


def test_an_unknown_board_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{BOARD}/", status_code=302, headers={"location": "http://www.hiringthing.com"},
    )

    with pytest.raises(CompanyNotFoundError):
        HiringThingScraper("acme").fetch()


def test_a_page_that_is_not_a_board_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=f"{BOARD}/", text="<html><body>Sign in</body></html>")

    with pytest.raises(ScraperError, match="unrecognized"):
        HiringThingScraper("acme").fetch()


@pytest.mark.parametrize("slug", ["", "acme.example", "https://www.hiringthing.com", "https://localhost"])
def test_rejects_what_is_not_a_board(slug: str) -> None:
    with pytest.raises(ScraperError):
        HiringThingScraper(slug)
