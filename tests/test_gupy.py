"""Tests for the Gupy multi-tenant scraper."""

from __future__ import annotations

import json

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import GupyScraper, ScraperRegistry

URL = "https://petz.gupy.io/"

PAGE_PROPS = {
    "careerPage": {
        "name": "Petz - O Melhor Ecossistema Pet",
        "publicationName": "Petz",
    },
    "jobs": [
        {
            "id": 1,
            "title": "Ajudante Geral",
            "type": "vacancy_type_effective",
            "department": "Operações",
            "workplace": {
                "address": {
                    "country": "Brasil",
                    "state": "São Paulo",
                    "city": "São Paulo",
                },
                "workplaceType": "on-site",
            },
            "quickApply": False,
        },
        {
            "id": 2,
            "title": "Estágio em Tecnologia",
            "type": "vacancy_type_internship",
            "department": "Tecnologia",
            "workplace": {
                "address": {"country": "Brasil", "city": "Remoto"},
                "workplaceType": "remote",
            },
            "quickApply": True,
        },
    ],
}


def wrap(page_props: dict) -> str:
    payload = {"props": {"pageProps": page_props}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload, ensure_ascii=False)}</script>"
    )


def test_registry_resolves_gupy() -> None:
    assert ScraperRegistry.get(ATSType.GUPY) is GupyScraper


def test_parses_listing(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap(PAGE_PROPS))
    jobs = GupyScraper("petz", include_descriptions=False).fetch()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.ats_id == "1"
    assert first.title == "Ajudante Geral"
    assert first.company == "Petz"
    assert first.ats_type is ATSType.GUPY
    assert first.global_id == "gupy:1"
    assert first.employment_type == "FULL_TIME"
    assert first.location == "São Paulo, São Paulo, Brasil"
    assert first.country_iso == "BR"
    assert first.region == "South America"
    assert first.language == "pt"
    assert first.is_remote is False
    assert str(first.url) == "https://petz.gupy.io/jobs/1"
    assert first.raw == {
        "quickApply": False,
        "workplace_type": "on-site",
        "vacancy_type": "vacancy_type_effective",
    }


@pytest.mark.asyncio
async def test_async_fetch(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap(PAGE_PROPS))
    jobs = await GupyScraper("petz", include_descriptions=False).afetch()
    assert [job.ats_id for job in jobs] == ["1", "2"]


def test_enriches_descriptions_when_enabled(httpx_mock) -> None:
    detail = {
        "job": {
            "description": "<p>Role intro</p>",
            "responsibilities": "<ul><li>Build things</li></ul>",
            "prerequisites": "Python &amp; APIs",
        }
    }
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": PAGE_PROPS["jobs"][:1]}))
    httpx_mock.add_response(url="https://petz.gupy.io/jobs/1", text=wrap(detail))

    jobs = GupyScraper("petz").fetch()

    assert jobs[0].description == "Role intro\n\nBuild things\n\nPython & APIs"


def test_get_description_fetches_one_detail(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap(PAGE_PROPS))
    job = GupyScraper("petz", include_descriptions=False).fetch()[0]
    httpx_mock.add_response(
        url="https://petz.gupy.io/jobs/1",
        text=wrap({"job": {"description": "<p>Full body</p>"}}),
    )

    assert GupyScraper("petz").get_description(job) == "Full body"


def test_returns_empty_live_tenant(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": []}))
    assert GupyScraper("petz", include_descriptions=False).fetch() == []


def test_company_name_falls_back_to_slug(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap({"jobs": PAGE_PROPS["jobs"][:1]}))
    jobs = GupyScraper("petz", include_descriptions=False).fetch()
    assert jobs[0].company == "petz"


def test_company_name_falls_back_to_career_page_name(httpx_mock) -> None:
    page = {
        "careerPage": {"name": "Petz Brasil"},
        "jobs": PAGE_PROPS["jobs"][:1],
    }
    httpx_mock.add_response(url=URL, text=wrap(page))
    jobs = GupyScraper("petz", include_descriptions=False).fetch()
    assert jobs[0].company == "Petz Brasil"


def test_deduplicates_jobs_by_id(httpx_mock) -> None:
    item = PAGE_PROPS["jobs"][0]
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": [item, item]}))
    jobs = GupyScraper("petz", include_descriptions=False).fetch()
    assert len(jobs) == 1


def test_skips_jobs_without_id_or_title(httpx_mock) -> None:
    jobs = [
        {"id": None, "title": "No id"},
        {"id": 1, "title": ""},
        PAGE_PROPS["jobs"][1],
    ]
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": jobs}))
    result = GupyScraper("petz", include_descriptions=False).fetch()
    assert [job.ats_id for job in result] == ["2"]


@pytest.mark.parametrize(
    ("vacancy_type", "expected"),
    [
        ("vacancy_type_effective", "FULL_TIME"),
        ("vacancy_type_associate", "FULL_TIME"),
        ("vacancy_type_legal_entity", "CONTRACT"),
        ("vacancy_legal_entity", "CONTRACT"),
        ("vacancy_type_outsource", "CONTRACT"),
        ("vacancy_type_internship", "INTERN"),
        ("vacancy_type_apprentice", "INTERN"),
        ("vacancy_type_temporary", "TEMPORARY"),
        ("vacancy_type_talent_pool", None),
    ],
)
def test_employment_type_mapping(httpx_mock, vacancy_type, expected) -> None:
    item = {**PAGE_PROPS["jobs"][0], "type": vacancy_type}
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": [item]}))
    job = GupyScraper("petz", include_descriptions=False).fetch()[0]
    assert job.employment_type == expected
    assert job.commitment == vacancy_type


@pytest.mark.parametrize(
    ("workplace_type", "expected"),
    [("remote", True), ("on-site", False), ("hybrid", None), (None, None)],
)
def test_remote_mapping(httpx_mock, workplace_type, expected) -> None:
    item = {
        **PAGE_PROPS["jobs"][0],
        "workplace": {
            "address": {"country": "Brasil"},
            "workplaceType": workplace_type,
        },
    }
    httpx_mock.add_response(url=URL, text=wrap({**PAGE_PROPS, "jobs": [item]}))
    job = GupyScraper("petz", include_descriptions=False).fetch()[0]
    assert job.is_remote is expected


def test_404_raises_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, status_code=404)
    with pytest.raises(CompanyNotFoundError):
        GupyScraper("petz", include_descriptions=False).fetch()


def test_missing_next_data_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text="<html></html>")
    with pytest.raises(ScraperError, match="__NEXT_DATA__"):
        GupyScraper("petz", include_descriptions=False).fetch()


def test_missing_jobs_array_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, text=wrap({"careerPage": {}}))
    with pytest.raises(ScraperError, match="missing jobs array"):
        GupyScraper("petz", include_descriptions=False).fetch()


def test_malformed_next_data_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=URL,
        text='<script id="__NEXT_DATA__" type="application/json">{</script>',
    )
    with pytest.raises(ScraperError, match="__NEXT_DATA__"):
        GupyScraper("petz", include_descriptions=False).fetch()


def test_next_data_attribute_order_is_flexible(httpx_mock) -> None:
    payload = json.dumps({"props": {"pageProps": PAGE_PROPS}})
    html = (
        '<script nonce="abc" type="application/json" id="__NEXT_DATA__">'
        f"{payload}</script>"
    )
    httpx_mock.add_response(url=URL, text=html)
    jobs = GupyScraper("petz", include_descriptions=False).fetch()
    assert len(jobs) == 2
