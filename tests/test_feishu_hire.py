"""Feishu Hire scraper."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import FeishuHireScraper, ScraperRegistry

BASE = "https://acme.jobs.feishu.cn"
TOKEN_URL = f"{BASE}/api/v1/csrf/token"
SEARCH_URL = f"{BASE}/api/v1/search/job/posts"


def _posting(job_id: str = "7001", **fields: object) -> dict:
    posting: dict = {
        "id": job_id,
        "title": "Backend Engineer",
        "description": "Build services.",
        "requirement": "Python.",
        "code": "A100",
        "publish_time": 1794564129001,
        "city_list": [
            {"name": "北京", "en_name": "Beijing"},
            {"name": "上海", "en_name": "Shanghai"},
        ],
        "recruit_type": {
            "name": "正式",
            "en_name": "Regular",
            "parent": {"name": "校招", "en_name": "Campus"},
        },
        "job_function": {"name": "算法", "en_name": "Algorithm"},
        "job_subject": {"name": {"zh_cn": "2027届校园招聘", "en_us": None}},
    }
    posting.update(fields)
    return posting


def _page(postings: list[dict], count: int | None = None) -> dict:
    return {
        "code": 0,
        "data": {
            "count": len(postings) if count is None else count,
            "job_post_list": postings,
        },
    }


def _site(httpx_mock, site: str, *pages: dict) -> None:
    """Register the token call and the search pages of one site."""
    headers = {"website-path": site}
    httpx_mock.add_response(
        method="POST",
        url=TOKEN_URL,
        match_headers=headers,
        json={"code": 0, "data": {"token": f"tok-{site}"}},
    )
    for page in pages:
        httpx_mock.add_response(
            method="POST", url=SEARCH_URL, match_headers=headers, json=page
        )


def test_registry_resolves_feishu_hire() -> None:
    assert ScraperRegistry.get(ATSType.FEISHU_HIRE) is FeishuHireScraper


def test_parses_a_posting(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([_posting()]))

    [job] = FeishuHireScraper("acme", company_name="Acme 科技").fetch()

    assert str(job.url) == f"{BASE}/index/position/7001/detail"
    assert job.title == "Backend Engineer"
    assert job.company == "Acme 科技"
    assert job.ats_type is ATSType.FEISHU_HIRE
    assert job.ats_id == "7001"
    assert job.location == "Beijing; Shanghai"
    assert job.department == "Algorithm"
    assert job.team == "2027届校园招聘"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "Regular"
    assert job.requisition_id == "A100"
    assert job.description == "Build services.\n\nPython."
    assert job.posted_at == datetime.fromtimestamp(1794564129.001, tz=UTC)
    assert job.raw == {"site": "index", "recruit_group": "Campus"}


def test_sends_the_site_and_the_token(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([_posting()]))

    FeishuHireScraper("acme").fetch()

    token_call, search_call = httpx_mock.get_requests()
    assert token_call.headers["website-path"] == "index"
    assert search_call.headers["x-csrf-token"] == "tok-index"
    assert search_call.headers["referer"] == f"{BASE}/index"


def test_company_defaults_to_the_tenant(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([_posting()]))

    [job] = FeishuHireScraper("acme").fetch()

    assert job.company == "acme"


def test_descriptions_can_be_left_out(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([_posting()]))

    [job] = FeishuHireScraper("acme", include_descriptions=False).fetch()

    assert job.description is None


def test_reads_every_page(httpx_mock) -> None:
    _site(
        httpx_mock,
        "index",
        _page([_posting("1"), _posting("2")], count=3),
        _page([_posting("3")], count=3),
    )

    jobs = FeishuHireScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["1", "2", "3"]
    offsets = [
        json.loads(request.content)["offset"]
        for request in httpx_mock.get_requests()
        if request.url.path.endswith("/posts")
    ]
    assert offsets == [0, 2]


def test_a_posting_listed_on_two_sites_is_kept_once(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([_posting("1"), _posting("2")]))
    _site(httpx_mock, "campus", _page([_posting("2"), _posting("3")]))

    jobs = FeishuHireScraper("acme", sites="index;campus").fetch()

    assert [(job.ats_id, job.raw["site"]) for job in jobs] == [
        ("1", "index"),
        ("2", "index"),
        ("3", "campus"),
    ]


def test_a_missing_site_is_skipped(httpx_mock) -> None:
    _site(httpx_mock, "index", {"code": -9000003, "message": "site not exist"})
    _site(httpx_mock, "campus", _page([_posting("3")]))

    jobs = FeishuHireScraper("acme", sites=["index", "campus"]).fetch()

    assert [job.ats_id for job in jobs] == ["3"]


def test_no_site_at_all_is_not_found(httpx_mock) -> None:
    _site(httpx_mock, "index", {"code": -9000003, "message": "site not exist"})

    with pytest.raises(CompanyNotFoundError, match="none of the sites index"):
        FeishuHireScraper("acme").fetch()


def test_an_empty_site_returns_no_jobs(httpx_mock) -> None:
    _site(httpx_mock, "index", _page([]))

    assert FeishuHireScraper("acme").fetch() == []


def test_an_api_error_is_reported(httpx_mock) -> None:
    _site(httpx_mock, "index", {"code": 500, "message": "boom"})

    with pytest.raises(ScraperError, match="error 500: boom"):
        FeishuHireScraper("acme").fetch()


def test_a_response_without_a_list_is_reported(httpx_mock) -> None:
    _site(httpx_mock, "index", {"code": 0, "data": {"count": 1}})

    with pytest.raises(ScraperError, match="omitted job_post_list"):
        FeishuHireScraper("acme").fetch()


def test_rows_without_an_id_or_a_title_are_dropped(httpx_mock) -> None:
    _site(
        httpx_mock,
        "index",
        _page([_posting(""), _posting("2", title=" "), _posting("3")]),
    )

    assert [job.ats_id for job in FeishuHireScraper("acme").fetch()] == ["3"]


@pytest.mark.parametrize(
    ("slug", "sites", "expected"),
    [
        ("acme", None, ("index",)),
        ("ACME.jobs.feishu.cn", None, ("index",)),
        ("https://acme.jobs.feishu.cn", "campus", ("campus",)),
        ("https://acme.jobs.feishu.cn/campus/position/7/detail", None, ("campus",)),
        ("https://acme.jobs.feishu.cn/campus", "index;campus", ("campus", "index")),
        ("acme", " index ; ;123456 ", ("index", "123456")),
    ],
)
def test_slug_and_sites(slug: str, sites: str | None, expected: tuple[str, ...]) -> None:
    scraper = FeishuHireScraper(slug, sites=sites)

    assert scraper.company_slug == "acme"
    assert scraper.base_url == BASE
    assert scraper.sites == expected


@pytest.mark.parametrize("slug", ["", "acme.example.com", "a/b", "https://feishu.cn/x"])
def test_an_unusable_slug_is_refused(slug: str) -> None:
    with pytest.raises(ScraperError, match="needs a tenant"):
        FeishuHireScraper(slug)


def test_an_unsafe_site_name_is_refused() -> None:
    with pytest.raises(ScraperError, match="unsafe site name"):
        FeishuHireScraper("acme", sites="index;../x")


def test_missing_optional_fields(httpx_mock) -> None:
    bare = {"id": "9", "title": "Designer"}
    _site(httpx_mock, "index", _page([bare]))

    [job] = FeishuHireScraper("acme").fetch()

    assert job.location is None
    assert job.department is None
    assert job.team is None
    assert job.employment_type is None
    assert job.requisition_id is None
    assert job.posted_at is None
    assert job.description is None
    assert job.raw == {"site": "index"}
