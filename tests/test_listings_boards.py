from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import BrighterMondayScraper, JobbermanScraper
from ats_scrapers.scrapers.base import ScraperRegistry

NG = "https://www.jobberman.com"
GH = "https://www.jobberman.com.gh"
KE = "https://www.brightermonday.co.ke"


def _index(site: str, names: list[str]) -> str:
    locs = "".join(
        f"<sitemap><loc>{site}/sitemap-listings-{name}-en.xml</loc></sitemap>"
        for name in names
    )
    return f'<?xml version="1.0"?><sitemapindex>{locs}</sitemapindex>'


def _sitemap(urls: list[str]) -> str:
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f'<?xml version="1.0"?><urlset>{locs}</urlset>'


def _job_page(site: str, number: int = 1266563, **fields: object) -> str:
    posting = {
        "@type": "JobPosting",
        "@id": f"{site}/#/schema/JobPosting/listing-{number}",
        "title": "Accountant",
        "description": "<div><b>Responsibilities:</b><ul><li>Keep the books.</li></ul></div>",
        "datePosted": "2026-10-05T00:00:00.000000Z",
        "validThrough": "2026-12-04T00:00:00.000000Z",
        "industry": "Retail, Fashion & FMCG",
        "occupationalCategory": "Accounting, Auditing & Finance",
        "employmentType": "FULL_TIME",
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "NGN",
            "value": {
                "@type": "QuantitativeValue",
                "value": 150000,
                "minValue": 150000,
                "maxValue": 250000,
                "unitText": "MONTH",
            },
        },
        "qualifications": "Mid level",
        "experienceRequirements": {"monthsOfExperience": 36},
        "jobLocation": {
            "@id": f"{site}/#/schema/Place/location-{number}",
            "address": {"addressCountry": "NG", "addressRegion": "Nigeria"},
        },
        "hiringOrganization": {"@id": f"{site}/#/schema/Organization/agency-{number}"},
    }
    posting.update(fields)
    graph = [
        {"@type": "WebPage", "@id": f"{site}/listings/accountant"},
        {"@type": "Organization", "@id": f"{site}/#/schema/Organization/1", "name": "The Board"},
        posting,
        {
            "@type": "Organization",
            "@id": f"{site}/#/schema/Organization/agency-{number}",
            "name": "Acme Stores",
        },
        {
            "@type": "Place",
            "@id": f"{site}/#/schema/Place/location-{number}",
            "address": {"@id": f"{site}/#/schema/PostalAddress/{number}"},
        },
        {
            "@type": "PostalAddress",
            "@id": f"{site}/#/schema/PostalAddress/{number}",
            "streetAddress": "Lagos",
            "addressLocality": "Nigeria",
            "addressRegion": "Marina",
            "addressCountry": "Nigeria",
        },
    ]
    data = json.dumps({"@context": "https://schema.org", "@graph": graph})
    return f'<html><script type="application/ld+json">{data}</script></html>'


def _site(httpx_mock, site: str, pages: dict[str, dict]) -> None:
    """Mock one site: one sitemap listing ``pages`` (URL -> response fields)."""
    httpx_mock.add_response(
        url=f"{site}/sitemap-listings-index-en.xml", text=_index(site, ["finance"])
    )
    httpx_mock.add_response(
        url=f"{site}/sitemap-listings-finance-en.xml", text=_sitemap(list(pages))
    )
    for url, response in pages.items():
        httpx_mock.add_response(url=url, **response)


def test_registry_resolves_the_boards() -> None:
    assert ScraperRegistry.get(ATSType.JOBBERMAN) is JobbermanScraper
    assert ScraperRegistry.get(ATSType.BRIGHTERMONDAY) is BrighterMondayScraper


def test_parses_a_job(httpx_mock) -> None:
    url = f"{NG}/listings/accountant-5pzme6"
    _site(httpx_mock, NG, {url: {"text": _job_page(NG)}})

    [job] = JobbermanScraper("ng").fetch()

    assert str(job.url) == url
    assert job.ats_id == "ng-1266563"
    assert job.global_id == "jobberman:ng-1266563"
    assert job.title == "Accountant"
    assert job.company == "Acme Stores"
    assert job.location == "Lagos, Nigeria"
    assert (job.country_iso, job.region) == ("NG", "Africa")
    assert (job.salary_min, job.salary_max) == (150000.0, 250000.0)
    assert (job.salary_currency, job.salary_period) == ("NGN", "MONTH")
    assert job.salary_summary == "NGN 150,000 - 250,000 per month"
    assert job.experience == 3
    assert job.employment_type == "FULL_TIME"
    assert job.department == "Accounting, Auditing & Finance"
    assert job.description == "Responsibilities:\nKeep the books."
    assert job.posted_at == datetime(2026, 10, 5, tzinfo=UTC)
    assert job.application_deadline == datetime(2026, 12, 4, tzinfo=UTC)
    assert job.raw == {"industry": "Retail, Fashion & FMCG", "level": "Mid level"}


def test_reads_every_sitemap_and_keeps_a_page_once(httpx_mock) -> None:
    first, second = f"{NG}/listings/a", f"{NG}/listings/b"
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-index-en.xml",
        text=_index(NG, ["finance", "sales"]),
    )
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-finance-en.xml",
        text=_sitemap([first, f"{NG}/jobs/accounting", "https://elsewhere.example/listings/x"]),
    )
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-sales-en.xml", text=_sitemap([first, second])
    )
    httpx_mock.add_response(url=first, text=_job_page(NG, 1))
    httpx_mock.add_response(url=second, text=_job_page(NG, 2))

    jobs = JobbermanScraper("ng").fetch()

    assert sorted(job.ats_id for job in jobs) == ["ng-1", "ng-2"]


def test_every_site_is_read_by_default(httpx_mock) -> None:
    _site(httpx_mock, NG, {f"{NG}/listings/a": {"text": _job_page(NG, 1)}})
    _site(httpx_mock, GH, {f"{GH}/listings/b": {"text": _job_page(GH, 1)}})

    jobs = JobbermanScraper("jobberman").fetch()

    assert [(job.ats_id, job.country_iso, job.location) for job in jobs] == [
        ("ng-1", "NG", "Lagos, Nigeria"),
        ("gh-1", "GH", "Lagos, Ghana"),
    ]


def test_a_site_address_names_one_site(httpx_mock) -> None:
    _site(httpx_mock, KE, {f"{KE}/listings/a": {"text": _job_page(KE, 7)}})

    [job] = BrighterMondayScraper("https://www.brightermonday.co.ke/jobs").fetch()

    assert job.ats_id == "ke-7"
    assert job.ats_type is ATSType.BRIGHTERMONDAY
    assert job.country_iso == "KE"


def test_a_job_closed_since_the_sitemap_is_dropped(httpx_mock) -> None:
    pages = {f"{NG}/listings/{n}": {"text": _job_page(NG, n)} for n in range(1, 11)}
    pages[f"{NG}/listings/gone"] = {"status_code": 404}
    pages[f"{NG}/listings/removed"] = {"status_code": 410}
    _site(httpx_mock, NG, pages)

    assert len(JobbermanScraper("ng").fetch()) == 10


def test_a_few_unreadable_pages_are_tolerated(httpx_mock) -> None:
    pages = {f"{NG}/listings/{n}": {"text": _job_page(NG, n)} for n in range(1, 11)}
    pages[f"{NG}/listings/odd"] = {"text": "<html>no record</html>"}
    _site(httpx_mock, NG, pages)

    assert len(JobbermanScraper("ng").fetch()) == 10


def test_many_unreadable_pages_are_reported(httpx_mock) -> None:
    pages = {f"{NG}/listings/{n}": {"text": "<html>no record</html>"} for n in range(3)}
    pages[f"{NG}/listings/ok"] = {"text": _job_page(NG)}
    _site(httpx_mock, NG, pages)

    with pytest.raises(ScraperError, match="3 of 4 job pages could not be read"):
        JobbermanScraper("ng").fetch()


def test_a_page_served_in_place_of_a_sitemap_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-index-en.xml", text=_index(NG, ["finance", "sales"])
    )
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-finance-en.xml", text=_sitemap([f"{NG}/listings/a"])
    )
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-sales-en.xml", text="<html>Be right back</html>"
    )

    with pytest.raises(ScraperError, match=r"sitemap-listings-sales-en\.xml is not a sitemap"):
        JobbermanScraper("ng").fetch()


def test_a_page_served_in_place_of_the_index_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-index-en.xml", text="<html>Be right back</html>"
    )

    with pytest.raises(ScraperError, match="is not a sitemap"):
        JobbermanScraper("ng").fetch()


def test_an_empty_sitemap_is_not_an_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-index-en.xml", text=_index(NG, ["finance", "sales"])
    )
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-finance-en.xml", text=_sitemap([f"{NG}/listings/a"])
    )
    httpx_mock.add_response(url=f"{NG}/sitemap-listings-sales-en.xml", text=_sitemap([]))
    httpx_mock.add_response(url=f"{NG}/listings/a", text=_job_page(NG))

    assert len(JobbermanScraper("ng").fetch()) == 1


def test_a_site_without_a_sitemap_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{NG}/sitemap-listings-index-en.xml", text="<sitemapindex></sitemapindex>"
    )

    with pytest.raises(ScraperError, match="lists no listings sitemap"):
        JobbermanScraper("ng").fetch()


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    _site(httpx_mock, NG, {f"{NG}/listings/a": {"text": _job_page(NG)}})

    [job] = JobbermanScraper("ng", include_descriptions=False).fetch()

    assert job.description is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("FULL_TIME", "FULL_TIME"),
        ("PART_TIME", "PART_TIME"),
        ("CONTRACTOR", "CONTRACT"),
        ("TEMPORARY", "TEMPORARY"),
        ("INTERN", "INTERN"),
        ("VOLUNTEER", None),
        (None, None),
    ],
)
def test_employment_types(httpx_mock, label: str | None, expected: str | None) -> None:
    page = _job_page(NG, employmentType=label)
    _site(httpx_mock, NG, {f"{NG}/listings/a": {"text": page}})

    [job] = JobbermanScraper("ng").fetch()

    assert job.employment_type == expected


def test_a_job_with_bare_fields(httpx_mock) -> None:
    page = _job_page(
        NG,
        baseSalary=None,
        experienceRequirements={"monthsOfExperience": 1},
        jobLocation=None,
        description=None,
        datePosted=None,
        validThrough="soon",
        industry=None,
        qualifications=None,
        occupationalCategory=None,
        jobLocationType="TELECOMMUTE",
    )
    _site(httpx_mock, NG, {f"{NG}/listings/a": {"text": page}})

    [job] = JobbermanScraper("ng").fetch()

    assert job.location == "Nigeria"
    assert job.is_remote is True
    assert job.salary_min is None
    assert job.salary_currency is None
    assert job.salary_summary is None
    assert job.experience is None
    assert job.description is None
    assert job.posted_at is None
    assert job.application_deadline is None
    assert job.raw is None


@pytest.mark.parametrize("label", ["Rest of Kenya", "Kenya", "Unspecified"])
def test_a_label_that_names_no_place_keeps_only_the_country(httpx_mock, label: str) -> None:
    page = _job_page(KE).replace('"streetAddress": "Lagos"', f'"streetAddress": "{label}"')
    _site(httpx_mock, KE, {f"{KE}/listings/a": {"text": page}})

    [job] = BrighterMondayScraper("ke").fetch()

    assert job.location == "Kenya"
    assert job.country_iso == "KE"


def test_a_job_abroad_names_no_country(httpx_mock) -> None:
    page = _job_page(KE).replace('"streetAddress": "Lagos"', '"streetAddress": "Outside Kenya"')
    _site(httpx_mock, KE, {f"{KE}/listings/a": {"text": page}})

    [job] = BrighterMondayScraper("ke").fetch()

    assert job.location == "Outside Kenya"
    assert job.country_iso is None
    assert job.region is None


def test_a_page_without_a_title_or_an_employer_counts_as_unreadable(httpx_mock) -> None:
    pages = {f"{NG}/listings/{n}": {"text": _job_page(NG, n)} for n in range(1, 21)}
    pages[f"{NG}/listings/untitled"] = {"text": _job_page(NG, 99, title=" ")}
    pages[f"{NG}/listings/anonymous"] = {
        "text": _job_page(NG, 98, hiringOrganization=None)
    }
    _site(httpx_mock, NG, pages)

    assert len(JobbermanScraper("ng").fetch()) == 20
