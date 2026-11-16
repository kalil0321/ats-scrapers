from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import EmploiticScraper
from ats_scrapers.scrapers.base import ScraperRegistry

SITE = "https://emploitic.com"
SITEMAP = f"{SITE}/sitemap-jobs.xml"
JOB = f"{SITE}/entreprises/gnt-telecom/offres-d-emploi/industries/magasinier-19a85"


def _sitemap(urls: list[str]) -> str:
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f'<?xml version="1.0"?><urlset>{locs}</urlset>'


def _job_page(**fields: object) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Magasinier",
        "description": "<h3>Description:</h3><p>Nous recherchons deux magasiniers.</p>",
        "datePosted": "2026-11-03T12:25:48.587Z",
        "validThrough": "2027-01-02T12:25:48.587Z",
        "employmentType": "TEMPORARY",
        "identifier": {
            "@type": "PropertyValue",
            "name": "Emploitic",
            "value": "5cd13739-161d-48c5-992d-68687665c448",
        },
        "educationRequirements": {"credentialCategory": "Niveau Terminal | Baccalauréat"},
        "experienceRequirements": "Sans Expérience | 1 À 2 Ans",
        "jobLocation": [
            {
                "@type": "Place",
                "address": {
                    "addressLocality": "Cheraga",
                    "addressRegion": "Alger",
                    "addressCountry": "DZ",
                },
            }
        ],
        "hiringOrganization": {"@type": "Organization", "name": "GNT Telecom"},
    }
    posting.update(fields)
    return (
        '<html><script type="application/ld+json">'
        f"{json.dumps(posting)}</script></html>"
    )


def _site(httpx_mock, pages: dict[str, dict]) -> None:
    httpx_mock.add_response(url=SITEMAP, text=_sitemap(list(pages)))
    for url, response in pages.items():
        httpx_mock.add_response(url=url, **response)


def _one(httpx_mock, **fields: object):
    _site(httpx_mock, {JOB: {"text": _job_page(**fields)}})
    [job] = EmploiticScraper("emploitic").fetch()
    return job


def test_registry_resolves_emploitic() -> None:
    assert ScraperRegistry.get(ATSType.EMPLOITIC) is EmploiticScraper


def test_parses_a_job(httpx_mock) -> None:
    job = _one(httpx_mock)

    assert str(job.url) == JOB
    assert job.ats_id == "5cd13739-161d-48c5-992d-68687665c448"
    assert job.title == "Magasinier"
    assert job.company == "GNT Telecom"
    assert job.location == "Cheraga, Alger, Algeria"
    assert (job.country_iso, job.region) == ("DZ", "Africa")
    assert job.employment_type == "TEMPORARY"
    assert job.description == "Description:\nNous recherchons deux magasiniers."
    assert job.posted_at == datetime(2026, 11, 3, 12, 25, 48, 587000, tzinfo=UTC)
    assert job.application_deadline == datetime(2027, 1, 2, 12, 25, 48, 587000, tzinfo=UTC)
    assert job.salary_min is None
    assert job.raw == {
        "experience": "Sans Expérience | 1 À 2 Ans",
        "education": "Niveau Terminal | Baccalauréat",
    }


def test_only_job_pages_of_the_site_are_read(httpx_mock) -> None:
    httpx_mock.add_response(
        url=SITEMAP,
        text=_sitemap([
            JOB,
            JOB,
            f"{SITE}/entreprises/gnt-telecom",
            "https://elsewhere.example/entreprises/x/offres-d-emploi/y/z",
        ]),
    )
    httpx_mock.add_response(url=JOB, text=_job_page())

    assert len(EmploiticScraper("emploitic").fetch()) == 1


def test_a_sitemap_without_job_pages_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=SITEMAP, text=_sitemap([f"{SITE}/entreprises/x"]))

    with pytest.raises(ScraperError, match="lists no job page"):
        EmploiticScraper("emploitic").fetch()


def test_a_page_served_in_place_of_the_sitemap_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=SITEMAP, text="<html>Be right back</html>")

    with pytest.raises(ScraperError, match="is not a sitemap"):
        EmploiticScraper("emploitic").fetch()


def test_a_job_closed_since_the_sitemap_is_dropped(httpx_mock) -> None:
    pages = {
        f"{JOB}-{n}": {"text": _job_page(identifier={"value": f"id-{n}"})}
        for n in range(10)
    }
    pages[f"{JOB}-gone"] = {"status_code": 404}
    pages[f"{JOB}-removed"] = {"status_code": 410}
    _site(httpx_mock, pages)

    assert len(EmploiticScraper("emploitic").fetch()) == 10


def test_many_unreadable_pages_are_reported(httpx_mock) -> None:
    pages = {f"{JOB}-{n}": {"text": "<html>no record</html>"} for n in range(3)}
    pages[JOB] = {"text": _job_page()}
    _site(httpx_mock, pages)

    with pytest.raises(ScraperError, match="3 of 4 job pages could not be read"):
        EmploiticScraper("emploitic").fetch()


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    _site(httpx_mock, {JOB: {"text": _job_page()}})

    [job] = EmploiticScraper("emploitic", include_descriptions=False).fetch()

    assert job.description is None


def test_several_places(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        jobLocation=[
            {"address": {"addressLocality": "Oran", "addressRegion": "Oran", "addressCountry": "DZ"}},
            {"address": {"addressRegion": "Alger", "addressCountry": "dz"}},
            {"address": {"addressLocality": "Oran", "addressRegion": "Oran", "addressCountry": "DZ"}},
        ],
    )

    assert job.location == "Oran, Algeria; Alger, Algeria"
    assert job.country_iso == "DZ"


def test_places_in_several_countries_name_no_country(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        jobLocation=[
            {"address": {"addressLocality": "Alger", "addressCountry": "DZ"}},
            {"address": {"addressLocality": "Tunis", "addressCountry": "TN"}},
        ],
    )

    assert job.location == "Alger, Algeria; Tunis"
    assert job.country_iso is None
    assert job.region is None


def test_a_job_with_bare_fields(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        identifier=None,
        jobLocation=None,
        employmentType=["OTHER", "FULL_TIME"],
        description=None,
        datePosted="soon",
        validThrough=None,
        educationRequirements=None,
        experienceRequirements=None,
        baseSalary={
            "currency": "DZD",
            "value": {"minValue": 60000, "maxValue": 90000, "unitText": "MONTH"},
        },
        jobLocationType="TELECOMMUTE",
    )

    assert job.ats_id == "magasinier-19a85"
    assert job.location is None
    assert job.country_iso is None
    assert job.is_remote is True
    assert job.employment_type == "FULL_TIME"
    assert job.description is None
    assert job.posted_at is None
    assert job.application_deadline is None
    assert job.salary_summary == "DZD 60,000 - 90,000 per month"
    assert (job.salary_min, job.salary_max, job.salary_period) == (60000.0, 90000.0, "MONTH")
    assert job.raw is None
