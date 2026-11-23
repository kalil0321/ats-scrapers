from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.resolve import resolve_careers_url
from ats_scrapers.scrapers import HireHiveScraper
from ats_scrapers.scrapers.base import ScraperRegistry

BASE = "https://acme.hirehive.com"
API = f"{BASE}/api/v2/jobs?page={{page}}&page_size=100"
HOME = '<html><title>Careers | Jobs at Acme &amp; Co.</title></html>'


def _job(job_id: str, **fields: object) -> dict:
    job: dict = {
        "id": job_id, "title": f"Job {job_id}", "location": "Bethpage", "state_code": "NY",
        "country": {"name": "United States", "code": "US"}, "salary": "$31.32 per hour",
        "description": {"html": "<p>Drive <b>safely</b></p>", "text": "Drive safely"},
        "category": {"id": "cat_1", "name": "Drivers"}, "type": {"type": "FullTime", "name": "Full Time"},
        "experience": {"type": "MidLevel", "name": "Mid Level"}, "language": {"name": "English", "code": "en-US"},
        "published_date": "2026-11-19T19:21:31.723Z", "created_date": "2026-11-19T18:56:19.44Z",
        "hosted_url": f"{BASE}/job-{job_id}", "compensation_tiers": [],
    }
    job.update(fields)
    return job


def _page(items: list[dict], *, total: int, page: int = 1, more: bool = False) -> dict:
    return {"meta": {"page_size": 100, "page": page, "total_items": total,
                     "total_pages": 2 if more else 1, "has_next_page": more}, "items": items}


def test_registry_and_address_resolve_hirehive() -> None:
    assert ScraperRegistry.get(ATSType.HIREHIVE) is HireHiveScraper
    resolved = resolve_careers_url("https://acme.hirehive.com/driver-bethpage-eL8Xma")
    assert resolved is not None and (resolved.ats, resolved.slug) == (ATSType.HIREHIVE, "acme")
    assert resolve_careers_url("https://www.hirehive.com/pricing") is None


def test_reads_a_board_with_its_company_and_every_field(httpx_mock) -> None:
    httpx_mock.add_response(url=API.format(page=1), json=_page([_job("job_a"), _job("job_b", state_code=None,
        location="Cork", country={"name": "Ireland", "code": "IE"}, salary=None, category=None,
        type={"type": "Contract", "name": "Contract"}, experience={"type": "NA", "name": "NA"})], total=2))
    httpx_mock.add_response(url=f"{BASE}/", text=HOME)

    first, second = HireHiveScraper("acme").fetch()

    assert first.global_id == "hirehive:job_a"
    assert (str(first.url), str(first.apply_url)) == (f"{BASE}/job-job_a",) * 2
    assert first.company == "Acme & Co."
    assert first.location == "Bethpage, NY, United States"
    assert (first.country_iso, first.job_category, first.employment_type) == ("US", "Drivers", "FULL_TIME")
    assert first.salary_summary == "$31.32 per hour"
    assert first.description == "Drive safely"
    assert first.posted_at == datetime(2026, 11, 19, 19, 21, 31, 723000, tzinfo=UTC)
    assert first.language == "en"
    assert first.raw == {"experience": "MidLevel"}
    assert (second.location, second.country_iso, second.employment_type) == ("Cork, Ireland", "IE", "CONTRACT")
    assert (second.salary_summary, second.job_category, second.raw) == (None, None, None)


def test_pages_are_followed_until_the_last(httpx_mock) -> None:
    first = [_job(f"j{i}") for i in range(100)]
    httpx_mock.add_response(url=API.format(page=1), json=_page(first, total=101, more=True))
    httpx_mock.add_response(url=API.format(page=2), json=_page([_job("j100")], total=101, page=2))
    httpx_mock.add_response(url=f"{BASE}/", text=HOME)

    assert len(HireHiveScraper("acme").fetch()) == 101


def test_a_count_that_disagrees_with_the_jobs_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=API.format(page=1), json=_page([_job("a"), _job("a"), {"id": "x"}], total=3))
    httpx_mock.add_response(url=f"{BASE}/", text=HOME)

    with pytest.raises(ScraperError, match="read 1 jobs but the site counts 3"):
        HireHiveScraper("acme").fetch()


def test_a_home_page_that_cannot_be_read_keeps_the_label_as_the_company(httpx_mock) -> None:
    httpx_mock.add_response(url=API.format(page=1), json=_page([_job("a")], total=1))
    httpx_mock.add_response(url=f"{BASE}/", text="<html>nothing</html>")

    assert HireHiveScraper("acme").fetch()[0].company == "acme"


def test_an_empty_board_and_an_unknown_company(httpx_mock) -> None:
    httpx_mock.add_response(url=API.format(page=1), json=_page([], total=0))
    httpx_mock.add_response(url=f"{BASE}/", text=HOME)
    assert HireHiveScraper("acme").fetch() == []

    httpx_mock.add_response(url="https://gone.hirehive.com/api/v2/jobs?page=1&page_size=100", status_code=404)
    with pytest.raises(CompanyNotFoundError):
        HireHiveScraper("gone").fetch()


def test_a_page_that_is_not_the_jobs_api_is_an_error(httpx_mock) -> None:
    httpx_mock.add_response(url=API.format(page=1), json={"unexpected": True})

    with pytest.raises(ScraperError, match="unrecognized page"):
        HireHiveScraper("acme").fetch()


@pytest.mark.parametrize("slug", ["", "a.b", "a/b", "evil.example.com/x"])
def test_a_slug_that_is_not_a_company_label_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match="invalid tenant slug"):
        HireHiveScraper(slug)
