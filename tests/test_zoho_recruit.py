"""Zoho Recruit scraper."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ScraperRegistry, ZohoRecruitScraper

PORTAL = "https://acme.zohorecruit.com/jobs/Careers"


def _job(job_id: str = "9001", **fields: object) -> dict:
    job: dict = {
        "id": job_id,
        "Posting_Title": "Site Engineer",
        "Job_Opening_Name": "Site Engineer (internal)",
        "City": "Lagos",
        "State": "Lagos",
        "Country": "Nigeria",
        "Industry": "Construction",
        "Job_Type": "Full time",
        "Remote_Job": False,
        "Publish": True,
        "Date_Opened": "2026-09-04",
        "Job_Description": "Run the site.",
    }
    job.update(fields)
    return job


def _hidden(element_id: str, value: object) -> str:
    encoded = html.escape(json.dumps(value, ensure_ascii=False), quote=True)
    return f'<input type="hidden" value="{encoded}" id="{element_id}">'


def _portal_page(jobs: object, company: str | None = "Acme Builders") -> str:
    parts = [_hidden("jobs", jobs)]
    if company is not None:
        parts.append(_hidden("meta", {"org_info": {"company_name": company}}))
    return f"<html><body>{''.join(parts)}</body></html>"


def _job_page(record: dict) -> str:
    literal = json.dumps([record]).replace("\\", "\\\\").replace('"', "\\x22")
    literal = literal.replace("-", "\\-").replace("'", "\\'")
    return f"<html><script>var jobs = JSON.parse('{literal}');</script></html>"


def test_registry_resolves_zoho_recruit() -> None:
    assert ScraperRegistry.get(ATSType.ZOHO_RECRUIT) is ZohoRecruitScraper


def test_parses_a_job(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text=_portal_page([_job()]))

    [job] = ZohoRecruitScraper(PORTAL).fetch()

    assert str(job.url) == f"{PORTAL}/9001"
    assert job.title == "Site Engineer"
    assert job.company == "Acme Builders"
    assert job.ats_type is ATSType.ZOHO_RECRUIT
    assert job.ats_id == "9001"
    assert job.location == "Lagos, Nigeria"
    assert job.is_remote is None
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Full time"
    assert job.description == "Run the site."
    assert job.posted_at == datetime(2026, 9, 4, tzinfo=UTC)
    assert job.raw == {"industry": "Construction"}


def test_the_catalogue_name_wins_over_the_page_name(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text=_portal_page([_job()]))

    [job] = ZohoRecruitScraper(PORTAL, company_name="Acme Group").fetch()

    assert job.company == "Acme Group"


def test_company_falls_back_to_the_tenant(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text=_portal_page([_job()], company=None))

    [job] = ZohoRecruitScraper(PORTAL).fetch()

    assert job.company == "acme"


def test_a_missing_text_is_read_from_the_job_page(httpx_mock) -> None:
    listed = _job(Job_Description=None, Date_Opened=None)
    full = _job(Job_Description="<p>It's a full-time role.</p>", Date_Opened="2026-11-12")
    httpx_mock.add_response(url=PORTAL, text=_portal_page([listed]))
    httpx_mock.add_response(url=f"{PORTAL}/9001", text=_job_page(full))

    [job] = ZohoRecruitScraper(PORTAL).fetch()

    assert job.description == "It's a full-time role."
    assert job.posted_at == datetime(2026, 11, 12, tzinfo=UTC)


def test_a_job_whose_page_cannot_be_read_is_kept(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL, text=_portal_page([_job(Job_Description=None)])
    )
    httpx_mock.add_response(url=f"{PORTAL}/9001", text="<html>maintenance</html>")

    [job] = ZohoRecruitScraper(PORTAL).fetch()

    assert job.description is None


MISSING_PAGE = "<html><head><title>Page does not exist</title></head></html>"


@pytest.mark.parametrize(
    "response", [{"status_code": 404}, {"text": MISSING_PAGE}]
)
def test_a_job_closed_since_the_list_was_read_is_dropped(httpx_mock, response: dict) -> None:
    httpx_mock.add_response(
        url=PORTAL, text=_portal_page([_job(Job_Description=None)])
    )
    httpx_mock.add_response(url=f"{PORTAL}/9001", **response)

    assert ZohoRecruitScraper(PORTAL).fetch() == []


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL, text=_portal_page([_job(Job_Description=None)])
    )

    [job] = ZohoRecruitScraper(PORTAL, include_descriptions=False).fetch()

    assert job.description is None
    assert len(httpx_mock.get_requests()) == 1


def test_get_description_reads_the_job_page(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL, text=_portal_page([_job(Job_Description=None)])
    )
    httpx_mock.add_response(
        url=f"{PORTAL}/9001", text=_job_page(_job(Job_Description="<p>Text</p>"))
    )
    scraper = ZohoRecruitScraper(PORTAL, include_descriptions=False)
    [job] = scraper.fetch()

    assert scraper.get_description(job) == "Text"


@pytest.mark.parametrize(
    ("job_type", "expected"),
    [
        ("Full time", "FULL_TIME"),
        ("Temps plein", "FULL_TIME"),
        ("Part time", "PART_TIME"),
        ("Temps partiel", "PART_TIME"),
        ("Contract", "CONTRACT"),
        ("Internship", "INTERN"),
        ("Temporary", "TEMPORARY"),
        ("Any", None),
        (None, None),
    ],
)
def test_employment_types(httpx_mock, job_type: str | None, expected: str | None) -> None:
    httpx_mock.add_response(url=PORTAL, text=_portal_page([_job(Job_Type=job_type)]))

    [job] = ZohoRecruitScraper(PORTAL).fetch()

    assert job.employment_type == expected
    assert job.commitment == job_type


def test_remote_and_bare_fields(httpx_mock) -> None:
    bare = {"id": "7", "Job_Opening_Name": "Analyst", "Remote_Job": True}
    httpx_mock.add_response(url=PORTAL, text=_portal_page([bare]))

    [job] = ZohoRecruitScraper(PORTAL, include_descriptions=False).fetch()

    assert job.title == "Analyst"
    assert job.location is None
    assert job.is_remote is True
    assert job.employment_type is None
    assert job.posted_at is None
    assert job.raw is None


def test_unpublished_and_incomplete_rows_are_dropped(httpx_mock) -> None:
    rows = [_job("1", Publish=False), {"id": "2"}, {"Posting_Title": "x"}, "junk", _job("5")]
    httpx_mock.add_response(url=PORTAL, text=_portal_page(rows))

    assert [job.ats_id for job in ZohoRecruitScraper(PORTAL).fetch()] == ["5"]


def test_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text=_portal_page([]))

    assert ZohoRecruitScraper(PORTAL).fetch() == []


def test_a_closed_portal_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        ZohoRecruitScraper(PORTAL).fetch()


def test_a_portal_that_does_not_exist_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text=MISSING_PAGE)

    with pytest.raises(CompanyNotFoundError, match="does not exist"):
        ZohoRecruitScraper(PORTAL).fetch()


def test_a_page_without_the_list_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=PORTAL, text="<html>maintenance</html>")

    with pytest.raises(ScraperError, match="carries no list of jobs"):
        ZohoRecruitScraper(PORTAL).fetch()


def test_unreadable_list_data_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(
        url=PORTAL, text='<input type="hidden" value="[{" id="jobs">'
    )

    with pytest.raises(ScraperError, match="unreadable 'jobs' data"):
        ZohoRecruitScraper(PORTAL).fetch()


@pytest.mark.parametrize(
    ("slug", "portal_url"),
    [
        ("acme.zohorecruit.com", PORTAL),
        ("https://ACME.zohorecruit.com", PORTAL),
        ("https://acme.zohorecruit.com/jobs/Careers/9001/Site-Engineer?source=CareerSite", PORTAL),
        ("https://acme.zohorecruit.eu/jobs/carrieres", "https://acme.zohorecruit.eu/jobs/carrieres"),
        ("https://acme.zohorecruit.in/jobs/Carri%C3%A8re", "https://acme.zohorecruit.in/jobs/Carri%C3%A8re"),
        ("https://acme.zohorecruit.com.au/recruit/Portal.na", "https://acme.zohorecruit.com.au/jobs/Careers"),
    ],
)
def test_slug_forms(slug: str, portal_url: str) -> None:
    scraper = ZohoRecruitScraper(slug)

    assert scraper.portal_url == portal_url
    assert scraper.company_slug == portal_url


@pytest.mark.parametrize(
    "slug", ["", "acme", "acme.example.com", "https://zohorecruit.com/jobs/Careers"]
)
def test_an_unusable_slug_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match="needs a"):
        ZohoRecruitScraper(slug)
