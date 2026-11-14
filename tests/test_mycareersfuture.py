from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import MyCareersFutureScraper
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.source_kind import source_kind
from pipeline.publisher import ATS_DEDUP_PRIORITY
from scripts.run_pipeline import CONFIGS

API = "https://api.mycareersfuture.gov.sg/v2/jobs"
SITE = "https://www.mycareersfuture.gov.sg"


def _posting(number: int = 1, **fields: object) -> dict:
    uuid = f"{number:032x}"
    posting = {
        "uuid": uuid,
        "title": "Senior Engineer",
        "description": "<p>About the role</p><ul><li>Python</li></ul>",
        "minimumYearsExperience": 6,
        "numberOfVacancies": 2,
        "categories": [{"id": 18, "category": "Information Technology"}],
        "employmentTypes": [
            {"id": 8, "employmentType": "Permanent"},
            {"id": 7, "employmentType": "Full Time"},
        ],
        "positionLevels": [{"id": 8, "position": "Senior Executive"}],
        "postedCompany": {"uen": "201912345M", "name": "ACME PTE. LTD."},
        "hiringCompany": None,
        "address": {
            "isOverseas": False,
            "overseasCountry": None,
            "districts": [{"id": 18, "location": "D18 Pasir Ris, Tampines"}],
            "lat": 1.3531,
            "lng": 103.9437,
        },
        "metadata": {
            "jobPostId": f"MCF-2026-{number:07d}",
            "newPostingDate": "2026-11-15",
            "expiryDate": "2026-12-15",
            "isHideSalary": False,
            "isHideEmployerName": False,
            "isHideHiringEmployerName": False,
            "jobDetailsUrl": f"{SITE}/job/information-technology/senior-engineer-acme-{uuid}",
        },
        "salary": {"minimum": 7000, "maximum": 10000, "type": {"salaryType": "Monthly"}},
    }
    posting.update(fields)
    return posting


def _feed(httpx_mock, pages: list[list[dict]], total: int | None = None) -> None:
    count = sum(len(page) for page in pages) if total is None else total
    for number, page in enumerate(pages):
        httpx_mock.add_response(
            url=f"{API}?limit=100&page={number}",
            json={"results": page, "total": count},
        )


def _one(httpx_mock, **fields: object):
    _feed(httpx_mock, [[_posting(**fields)], []])
    [job] = MyCareersFutureScraper("all").fetch()
    return job


def test_registry_resolves_mycareersfuture() -> None:
    assert ScraperRegistry.get(ATSType.MYCAREERSFUTURE) is MyCareersFutureScraper


def test_parses_a_posting(httpx_mock) -> None:
    job = _one(httpx_mock)

    assert job.ats_id == f"{1:032x}"
    assert job.global_id == f"mycareersfuture:{1:032x}"
    assert str(job.url) == (
        f"{SITE}/job/information-technology/senior-engineer-acme-{1:032x}"
    )
    assert job.title == "Senior Engineer"
    assert job.company == "ACME PTE. LTD."
    assert job.location == "Pasir Ris, Tampines, Singapore"
    assert (job.country_iso, job.region) == ("SG", "Asia")
    assert (job.lat, job.lon) == (1.3531, 103.9437)
    assert (job.salary_min, job.salary_max) == (7000.0, 10000.0)
    assert (job.salary_currency, job.salary_period) == ("SGD", "MONTH")
    assert job.salary_summary == "SGD 7,000 - 10,000 Monthly"
    assert job.experience == 6
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Permanent, Full Time"
    assert job.department == "Information Technology"
    assert job.requisition_id == "MCF-2026-0000001"
    assert job.description == "About the role\nPython"
    assert job.posted_at == datetime(2026, 11, 15, tzinfo=UTC)
    assert job.application_deadline == datetime(2026, 12, 15, tzinfo=UTC)
    assert job.language == "en"
    assert job.raw == {
        "position_levels": ["Senior Executive"],
        "categories": ["Information Technology"],
        "vacancies": 2,
    }


def test_reads_every_page_and_drops_repeats(httpx_mock) -> None:
    first = [_posting(number) for number in range(1, 101)]
    second = [_posting(100), _posting(101), _posting(102)]
    _feed(httpx_mock, [first, second, []], total=102)

    jobs = MyCareersFutureScraper("all").fetch()

    assert len(jobs) == 102
    assert len({job.ats_id for job in jobs}) == 102


def test_reads_on_when_the_feed_grew(httpx_mock) -> None:
    first = [_posting(number) for number in range(1, 101)]
    _feed(httpx_mock, [first, [_posting(101)], []], total=100)

    assert len(MyCareersFutureScraper("all").fetch()) == 101


def test_an_empty_feed(httpx_mock) -> None:
    _feed(httpx_mock, [[]])

    assert MyCareersFutureScraper("all").fetch() == []


def test_a_short_read_is_reported(httpx_mock) -> None:
    _feed(httpx_mock, [[_posting(1)], []], total=40)

    with pytest.raises(ScraperError, match="returned 1 of 40 postings"):
        MyCareersFutureScraper("all").fetch()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"results": None, "total": 1},
        {"results": [], "total": "1"},
        {"results": [], "total": True},
    ],
)
def test_an_unexpected_page_is_reported(httpx_mock, payload: object) -> None:
    httpx_mock.add_response(url=f"{API}?limit=100&page=0", json=payload)

    with pytest.raises(ScraperError, match="page 0 is not a list of postings"):
        MyCareersFutureScraper("all").fetch()


def test_the_hiring_employer_is_named(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        hiringCompany={"uen": "T01", "name": "CLIENT PTE. LTD."},
        postedCompany={"uen": "T02", "name": "AGENCY PTE. LTD."},
    )

    assert job.company == "CLIENT PTE. LTD."
    assert job.raw is not None
    assert job.raw["posted_by"] == "AGENCY PTE. LTD."


def test_a_hidden_hiring_employer_falls_back_to_the_poster(httpx_mock) -> None:
    posting = _posting(hiringCompany={"name": "CLIENT PTE. LTD."})
    posting["metadata"]["isHideHiringEmployerName"] = True
    _feed(httpx_mock, [[posting], []])

    [job] = MyCareersFutureScraper("all").fetch()

    assert job.company == "ACME PTE. LTD."
    assert "posted_by" not in (job.raw or {})


def test_postings_without_an_employer_an_id_or_a_title_are_dropped(httpx_mock) -> None:
    hidden = _posting(2)
    hidden["metadata"]["isHideEmployerName"] = True
    _feed(
        httpx_mock,
        [[_posting(1), hidden, _posting(3, uuid=""), _posting(4, title=" ")], []],
        total=1,
    )

    assert [job.ats_id for job in MyCareersFutureScraper("all").fetch()] == [f"{1:032x}"]


def test_a_hidden_salary_is_left_out(httpx_mock) -> None:
    posting = _posting()
    posting["metadata"]["isHideSalary"] = True
    _feed(httpx_mock, [[posting], []])

    [job] = MyCareersFutureScraper("all").fetch()

    assert job.salary_min is None
    assert job.salary_max is None
    assert job.salary_currency is None
    assert job.salary_period is None
    assert job.salary_summary is None


def test_a_single_salary_amount(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        salary={"minimum": 12, "maximum": 12, "type": {"salaryType": "Hourly"}},
    )

    assert job.salary_summary == "SGD 12 Hourly"
    assert job.salary_period == "HOUR"


@pytest.mark.parametrize(
    ("districts", "expected"),
    [
        ([{"location": "Islandwide"}], "Singapore"),
        ([], "Singapore"),
        ([{"location": "D01 Cecil, Marina"}], "Cecil, Marina, Singapore"),
        ([{"location": "Central"}], "Central, Singapore"),
    ],
)
def test_places(httpx_mock, districts: list[dict], expected: str) -> None:
    job = _one(httpx_mock, address={"isOverseas": False, "districts": districts})

    assert job.location == expected
    assert job.country_iso == "SG"
    assert job.lat is None
    assert job.lon is None


def test_a_posting_abroad_keeps_its_country(httpx_mock) -> None:
    job = _one(
        httpx_mock,
        address={"isOverseas": True, "overseasCountry": "Malaysia", "districts": []},
    )

    assert job.location == "Malaysia"
    assert job.country_iso is None
    assert job.region is None


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["Full Time"], "FULL_TIME"),
        (["Permanent"], "FULL_TIME"),
        (["Part Time", "Permanent"], "PART_TIME"),
        (["Contract", "Full Time"], "CONTRACT"),
        (["Internship/Attachment", "Full Time"], "INTERN"),
        (["Temporary"], "TEMPORARY"),
        (["Freelance"], "CONTRACT"),
        (["Flexi-work"], None),
        ([], None),
    ],
)
def test_employment_types(httpx_mock, labels: list[str], expected: str | None) -> None:
    job = _one(
        httpx_mock,
        employmentTypes=[{"employmentType": label} for label in labels],
    )

    assert job.employment_type == expected


def test_a_posting_with_bare_fields(httpx_mock) -> None:
    _feed(
        httpx_mock,
        [[{"uuid": "abc", "title": "Clerk", "postedCompany": {"name": "Shop"}}], []],
    )

    [job] = MyCareersFutureScraper("all").fetch()

    assert str(job.url) == f"{SITE}/job/abc"
    assert job.location == "Singapore"
    assert job.description is None
    assert job.posted_at is None
    assert job.experience is None
    assert job.raw is None


def test_a_link_to_another_site_is_not_used(httpx_mock) -> None:
    posting = _posting()
    posting["metadata"]["jobDetailsUrl"] = "https://example.com/job/1"
    _feed(httpx_mock, [[posting], []])

    [job] = MyCareersFutureScraper("all").fetch()

    assert str(job.url) == f"{SITE}/job/{1:032x}"


def test_mycareersfuture_is_a_public_board() -> None:
    config = CONFIGS["mycareersfuture"]

    assert config["scraper"] is MyCareersFutureScraper
    assert config["singleton"] is True
    assert config["output"] == "mycareersfuture/jobs.csv"
    assert config["fail_closed_on_empty"] is True
    assert source_kind("mycareersfuture") == "gov_board"
    assert ATS_DEDUP_PRIORITY["mycareersfuture"] == ATS_DEDUP_PRIORITY["jobbankca"]
    assert re.fullmatch(r"[a-z]+", ATSType.MYCAREERSFUTURE.value)
