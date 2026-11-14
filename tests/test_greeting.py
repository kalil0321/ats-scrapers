"""Greeting scraper."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import GreetingScraper, ScraperRegistry

BASE = "https://acme.career.greetinghr.com"
HOME = f"{BASE}/"


def _position(**fields: object) -> dict:
    position: dict = {
        "workspaceOccupation": {"occupation": "개발"},
        "workspacePlace": {
            "location": "본사",
            "place": "서울특별시 강남구 테헤란로 427",
            "workFromHome": False,
            "geo": {"lat": 37.5, "lng": 127.05},
        },
        "jobPositionCareer": {"careerType": "EXPERIENCED"},
        "jobPositionEmployment": {"employmentType": "FULL_TIME_WORKER"},
    }
    position.update(fields)
    return position


def _opening(opening_id: int = 1001, **fields: object) -> dict:
    opening: dict = {
        "openingId": opening_id,
        "title": "백엔드 개발자",
        "openDate": "2026-01-08T09:35:57Z",
        "dueDate": "2026-12-31T14:59:59Z",
        "openingJobPosition": {"openingJobPositions": [_position()]},
    }
    opening.update(fields)
    return opening


def _page(queries: list[dict]) -> str:
    data = {"props": {"pageProps": {"dehydratedState": {"queries": queries}}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data, ensure_ascii=False)}</script></html>"
    )


def _home(openings: object, workspace: str | None = "에이크미") -> str:
    queries: list[dict] = [{"queryKey": ["openings"], "state": {"data": openings}}]
    if workspace is not None:
        queries.insert(0, {
            "queryKey": ["publicCareer", "getCareerBaseInfo", "acme"],
            "state": {"data": {"workspace": {"id": 1, "name": workspace}}},
        })
    return _page(queries)


def _detail(detail: str | None = "<p>업무 내용</p>") -> str:
    info = {"openingId": 1001, "status": "OPEN", "detail": detail}
    return _page([{
        "queryKey": ["career", "getOpeningById", {"openingId": 1001}],
        "state": {"data": {"success": True, "data": {"openingsInfo": json.dumps(info)}}},
    }])


def test_registry_resolves_greeting() -> None:
    assert ScraperRegistry.get(ATSType.GREETING) is GreetingScraper


def test_parses_an_opening(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening()]))
    httpx_mock.add_response(url=f"{BASE}/o/1001", text=_detail())

    [job] = GreetingScraper("acme").fetch()

    assert str(job.url) == f"{BASE}/o/1001"
    assert job.title == "백엔드 개발자"
    assert job.company == "에이크미"
    assert job.ats_type is ATSType.GREETING
    assert job.ats_id == "1001"
    assert job.location == "서울특별시 강남구 테헤란로 427"
    assert job.country_iso == "KR"
    assert (job.lat, job.lon) == (37.5, 127.05)
    assert job.is_remote is None
    assert job.department == "개발"
    assert job.employment_type == "FULL_TIME"
    assert job.posted_at == datetime(2026, 1, 8, 9, 35, 57, tzinfo=UTC)
    assert job.application_deadline == datetime(2026, 12, 31, 14, 59, 59, tzinfo=UTC)
    assert job.description == "<p>업무 내용</p>"
    assert job.raw == {
        "career_types": ["EXPERIENCED"],
        "employment_types": ["FULL_TIME_WORKER"],
    }


def test_the_catalogue_name_wins_over_the_workspace_name(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening()]))

    [job] = GreetingScraper(
        "acme", company_name="Acme Korea", include_descriptions=False
    ).fetch()

    assert job.company == "Acme Korea"


def test_company_falls_back_to_the_tenant(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening()], workspace=None))

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.company == "acme"


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening()]))

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.description is None
    assert len(httpx_mock.get_requests()) == 1


def test_an_opening_whose_page_cannot_be_read_is_kept(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening(), _opening(1002)]))
    httpx_mock.add_response(url=f"{BASE}/o/1001", status_code=404)
    httpx_mock.add_response(url=f"{BASE}/o/1002", text=_detail("<p>두번째</p>"))

    jobs = GreetingScraper("acme").fetch()

    assert [(job.ats_id, job.description) for job in jobs] == [
        ("1001", None),
        ("1002", "<p>두번째</p>"),
    ]


def test_get_description_reads_the_opening_page(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([_opening()]))
    httpx_mock.add_response(url=f"{BASE}/o/1001", text=_detail())
    scraper = GreetingScraper("acme", include_descriptions=False)
    [job] = scraper.fetch()

    assert scraper.get_description(job) == "<p>업무 내용</p>"


def test_several_places_and_remote_work(httpx_mock) -> None:
    busan = _position(workspacePlace={"place": "부산광역시 해운대구", "workFromHome": True})
    opening = _opening(openingJobPosition={"openingJobPositions": [_position(), busan]})
    httpx_mock.add_response(url=HOME, text=_home([opening]))

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.location == "서울특별시 강남구 테헤란로 427; 부산광역시 해운대구"
    assert job.country_iso == "KR"
    assert job.is_remote is True


def test_the_place_label_is_used_without_an_address(httpx_mock) -> None:
    position = _position(workspacePlace={"location": "Singapore", "place": None})
    opening = _opening(openingJobPosition={"openingJobPositions": [position]})
    httpx_mock.add_response(url=HOME, text=_home([opening]))

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.location == "Singapore"
    assert job.country_iso == "SG"
    assert job.lat is None


@pytest.mark.parametrize(
    ("employment", "expected"),
    [
        ("CONTRACT_WORKER", "CONTRACT"),
        ("INTERN_WORKER", "INTERN"),
        ("PART_TIME_WORKER", "PART_TIME"),
        ("MILITARY_SERVICE_EXCEPTION", None),
        (None, None),
    ],
)
def test_employment_types(httpx_mock, employment: str | None, expected: str | None) -> None:
    position = _position(jobPositionEmployment={"employmentType": employment})
    opening = _opening(openingJobPosition={"openingJobPositions": [position]})
    httpx_mock.add_response(url=HOME, text=_home([opening]))

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.employment_type == expected


def test_an_opening_with_bare_fields(httpx_mock) -> None:
    httpx_mock.add_response(
        url=HOME, text=_home([{"openingId": 7, "title": "디자이너"}])
    )

    [job] = GreetingScraper("acme", include_descriptions=False).fetch()

    assert job.location is None
    assert job.country_iso is None
    assert job.department is None
    assert job.employment_type is None
    assert job.posted_at is None
    assert job.raw is None


def test_rows_without_an_id_or_a_title_are_dropped(httpx_mock) -> None:
    openings = [{"title": "no id"}, {"openingId": 2, "title": " "}, "junk", _opening(3)]
    httpx_mock.add_response(url=HOME, text=_home(openings))

    jobs = GreetingScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["3"]


def test_no_openings(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_home([]))

    assert GreetingScraper("acme").fetch() == []


def test_a_closed_site_is_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, status_code=404)

    with pytest.raises(CompanyNotFoundError):
        GreetingScraper("acme").fetch()


def test_a_page_without_data_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text="<html>maintenance</html>")

    with pytest.raises(ScraperError, match="carries no page data"):
        GreetingScraper("acme").fetch()


def test_a_page_without_the_list_is_reported(httpx_mock) -> None:
    httpx_mock.add_response(url=HOME, text=_page([]))

    with pytest.raises(ScraperError, match="omitted the list of openings"):
        GreetingScraper("acme").fetch()


@pytest.mark.parametrize(
    "slug",
    [
        "acme",
        "ACME.career.greetinghr.com",
        "https://acme.career.greetinghr.com/ko/o/1001",
    ],
)
def test_slug_forms(slug: str) -> None:
    scraper = GreetingScraper(slug)

    assert scraper.company_slug == "acme"
    assert scraper.base_url == BASE


@pytest.mark.parametrize("slug", ["", "acme.example.com", "https://greetinghr.com/x"])
def test_an_unusable_slug_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match="needs a tenant"):
        GreetingScraper(slug)
