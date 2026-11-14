"""Manatal scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import ManatalScraper, ScraperRegistry
from ats_scrapers.scrapers import manatal as manatal_module

API = "https://www.careers-page.com/api/v1.0/c/acme"
ORG = f"{API}/"
JOBS = f"{API}/jobs/?page_size=20"


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch) -> None:
    monkeypatch.setattr(manatal_module, "PAGE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(manatal_module, "REFUSAL_WAIT_SECONDS", 0)


def _job(job_hash: str = "939W9Y76", **fields: object) -> dict:
    job: dict = {
        "id": 3194995,
        "hash": job_hash,
        "position_name": "Site Engineer",
        "description": "<p><strong>Role:</strong> run the site.</p><ul><li>Safety</li></ul>",
        "country": "Kenya",
        "state": "",
        "city": "Nairobi",
        "location_display": "Nairobi, Kenya",
    }
    job.update(fields)
    return job


def _page(jobs: list, next_url: str | None = None) -> dict:
    return {"count": len(jobs), "next": next_url, "previous": None, "results": jobs}


def test_registry_resolves_manatal() -> None:
    assert ScraperRegistry.get(ATSType.MANATAL) is ManatalScraper


def test_parses_a_job(httpx_mock) -> None:
    httpx_mock.add_response(url=ORG, json={"name": "Acme Builders"})
    httpx_mock.add_response(url=JOBS, json=_page([_job()]))

    [job] = ManatalScraper("acme").fetch()

    assert str(job.url) == "https://www.careers-page.com/acme/job/939W9Y76"
    assert job.title == "Site Engineer"
    assert job.company == "Acme Builders"
    assert job.ats_type is ATSType.MANATAL
    assert job.ats_id == "939W9Y76"
    assert job.requisition_id == "3194995"
    assert job.location == "Nairobi, Kenya"
    assert job.description == "Role: run the site.\nSafety"


def test_the_catalogue_name_saves_the_organisation_call(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, json=_page([_job()]))

    [job] = ManatalScraper("acme", company_name="Acme Group").fetch()

    assert job.company == "Acme Group"
    assert [str(r.url) for r in httpx_mock.get_requests()] == [JOBS]


def test_company_falls_back_to_the_slug(httpx_mock) -> None:
    httpx_mock.add_response(url=ORG, json={"name": " "})
    httpx_mock.add_response(url=JOBS, json=_page([_job()]))

    [job] = ManatalScraper("acme").fetch()

    assert job.company == "acme"


def test_reads_every_page_and_keeps_a_job_once(httpx_mock) -> None:
    second = f"{API}/jobs/?page=2&page_size=20"
    httpx_mock.add_response(url=JOBS, json=_page([_job("A"), _job("B")], next_url=second))
    httpx_mock.add_response(url=second, json=_page([_job("B"), _job("C")]))

    jobs = ManatalScraper("acme", company_name="Acme").fetch()

    assert [job.ats_id for job in jobs] == ["A", "B", "C"]


def test_a_read_that_skipped_jobs_is_reported(httpx_mock) -> None:
    page = _page([_job("A")])
    page["count"] = 40
    httpx_mock.add_response(url=JOBS, json=page)

    with pytest.raises(ScraperError, match="returned 1 of 40 jobs"):
        ManatalScraper("acme", company_name="Acme").fetch()


def test_a_few_jobs_closed_during_the_read_are_tolerated(httpx_mock) -> None:
    page = _page([_job("A")])
    page["count"] = 3
    httpx_mock.add_response(url=JOBS, json=page)

    assert len(ManatalScraper("acme", company_name="Acme").fetch()) == 1


def test_a_next_page_on_another_host_is_not_followed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=JOBS, json=_page([_job()], next_url="https://elsewhere.example/jobs/?page=2")
    )

    assert len(ManatalScraper("acme", company_name="Acme").fetch()) == 1


def test_a_temporary_refusal_is_waited_out(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, status_code=403)
    httpx_mock.add_response(url=JOBS, json=_page([_job()]))

    assert len(ManatalScraper("acme", company_name="Acme").fetch()) == 1


def test_a_lasting_refusal_is_reported(httpx_mock) -> None:
    for _ in range(manatal_module.MAX_REFUSALS):
        httpx_mock.add_response(url=JOBS, status_code=403)

    with pytest.raises(ScraperError, match="kept refusing requests"):
        ManatalScraper("acme", company_name="Acme").fetch()


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, json=_page([_job()]))

    [job] = ManatalScraper("acme", company_name="Acme", include_descriptions=False).fetch()

    assert job.description is None


def test_place_is_built_from_its_parts_when_needed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=JOBS, json=_page([_job(location_display="", state="Nairobi County")])
    )

    [job] = ManatalScraper("acme", company_name="Acme").fetch()

    assert job.location == "Nairobi, Nairobi County, Kenya"


def test_rows_without_a_hash_or_a_title_are_dropped(httpx_mock) -> None:
    rows = [_job(""), _job("B", position_name=" "), "junk", _job("C", id="x")]
    httpx_mock.add_response(url=JOBS, json=_page(rows))

    [job] = ManatalScraper("acme", company_name="Acme").fetch()

    assert job.ats_id == "C"
    assert job.requisition_id is None


def test_no_jobs(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, json=_page([]))

    assert ManatalScraper("acme", company_name="Acme").fetch() == []


def test_an_unknown_page_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        ManatalScraper("acme", company_name="Acme").fetch()


def test_a_response_without_a_list_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=JOBS, json={"detail": "oops"})

    with pytest.raises(ScraperError, match="returned no list of jobs"):
        ManatalScraper("acme", company_name="Acme").fetch()


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("acme", "acme"),
        ("-black-pen-recruitment", "-black-pen-recruitment"),
        ("https://www.careers-page.com/acme", "acme"),
        ("https://careers-page.com/acme/job/939W9Y76", "acme"),
    ],
)
def test_slug_forms(slug: str, expected: str) -> None:
    scraper = ManatalScraper(slug)

    assert scraper.company_slug == expected
    assert scraper.board_url == f"https://www.careers-page.com/{expected}"


@pytest.mark.parametrize(
    "slug", ["", "a/b", "https://www.careers-page.com/", "https://example.com/acme"]
)
def test_an_unusable_slug_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match=r"needs a careers-page\.com page"):
        ManatalScraper(slug)
