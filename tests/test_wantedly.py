from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import WantedlyScraper
from ats_scrapers.scrapers.base import ScraperRegistry

SLUG = "Acme_Japan"
API = re.compile(r"^https://www\.wantedly\.com/api/v1/projects\?")


def _project(project_id: int, **overrides: object) -> dict[str, object]:
    """One project as the listing gives it, staff members included."""
    item: dict[str, object] = {
        "id": project_id,
        "title": f"  バックエンド  エンジニア {project_id} ",
        "published_at": "2026-10-14T19:54:53.043+09:00",
        "location": "東京都渋谷区恵比寿四丁目20番3号",
        "location_suffix": "恵比寿ガーデンプレイスタワー5階",
        "description": "サービスを開発します。\r\n\r\n\r\n\r\nAPIも  設計します。 ",
        "looking_for": "エンジニア",
        "company": {
            "id": 180819,
            "name": "株式会社アクメ",
            "founder": "山田 太郎",
            "url": "https://acme.example",
        },
        "staffings": [{"user_id": 1, "name": "東郷 桃子", "description": "在宅ライター"}],
        "leader": {"name_ja": "東郷 桃子", "name_en": "Momoko Togo"},
    }
    item.update(overrides)
    return item


def _listing(
    projects: list[dict[str, object]],
    *,
    total: int | None = None,
    pages: int | None = None,
) -> dict[str, object]:
    count = len(projects) if total is None else total
    return {
        "data": projects,
        "_metadata": {
            "total_objects": count,
            "per_page": 10,
            "total_pages": pages if pages is not None else max(1, -(-count // 10)),
        },
    }


def _asked(httpx_mock) -> list[dict[str, str]]:
    return [dict(request.url.params) for request in httpx_mock.get_requests()]


def test_registry_resolves_wantedly() -> None:
    assert ScraperRegistry.get(ATSType.WANTEDLY) is WantedlyScraper


def test_lists_the_projects_of_one_company(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_project(2599130)]))

    jobs = WantedlyScraper(SLUG).fetch()

    assert _asked(httpx_mock) == [{"company_id": SLUG, "per_page": "10", "page": "1"}]
    request = httpx_mock.get_requests()[0]
    assert request.headers["X-Requested-With"] == "XMLHttpRequest"
    (job,) = jobs
    assert job.ats_id == "2599130"
    assert job.global_id == "wantedly:2599130"
    assert str(job.url) == "https://www.wantedly.com/projects/2599130"
    assert job.title == "バックエンド エンジニア 2599130"
    assert job.company == "株式会社アクメ"
    assert job.location == (
        "東京都渋谷区恵比寿四丁目20番3号 恵比寿ガーデンプレイスタワー5階"
    )
    assert job.description == "サービスを開発します。\n\nAPIも 設計します。"
    assert job.posted_at == datetime(2026, 10, 14, 10, 54, 53, 43000, tzinfo=UTC)
    assert job.fetched_at is not None
    assert job.raw == {"company_slug": SLUG}


def test_nothing_about_a_person_is_stored(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_project(1)]))

    job = WantedlyScraper(SLUG).fetch()[0]

    stored = job.model_dump_json()
    for personal in ("東郷", "Momoko", "山田", "在宅ライター"):
        assert personal not in stored


def test_catalogue_name_is_used_for_the_company(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_project(1)]))

    job = WantedlyScraper(SLUG, company_name=" Acme Japan ").fetch()[0]

    assert job.company == "Acme Japan"


def test_listing_only_leaves_the_description_out(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_project(1)]))

    job = WantedlyScraper(SLUG, include_descriptions=False).fetch()[0]

    assert job.description is None


def test_company_without_project_gives_an_empty_list(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([]))

    assert WantedlyScraper(SLUG).fetch() == []


def test_every_page_is_read(httpx_mock) -> None:
    first = [_project(i) for i in range(1, 11)]
    second = [_project(i) for i in range(11, 15)]
    httpx_mock.add_response(url=API, json=_listing(first, total=14))
    httpx_mock.add_response(url=API, json=_listing(second, total=14))

    jobs = WantedlyScraper(SLUG).fetch()

    assert [params["page"] for params in _asked(httpx_mock)] == ["1", "2"]
    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 15))


def test_pages_are_read_again_when_the_moving_order_skips_a_project(httpx_mock) -> None:
    """The listing's order moves between two requests: project 10 shows on
    both pages of the first pass and project 11 on none."""
    first_pass = [
        _listing([_project(i) for i in range(1, 11)], total=12),
        _listing([_project(10), _project(12)], total=12),
    ]
    second_pass = [
        _listing([_project(i) for i in range(1, 11)], total=12),
        _listing([_project(11), _project(12)], total=12),
    ]
    for payload in first_pass + second_pass:
        httpx_mock.add_response(url=API, json=payload)

    jobs = WantedlyScraper(SLUG).fetch()

    assert [params["page"] for params in _asked(httpx_mock)] == ["1", "2", "1", "2"]
    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 13))


def test_list_far_short_of_the_announced_total_is_refused(httpx_mock) -> None:
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 11)], total=40, pages=1)
        )

    with pytest.raises(ScraperError, match="announces 40 projects and listed 10"):
        WantedlyScraper(SLUG).fetch()


def test_list_close_to_the_announced_total_is_kept(httpx_mock) -> None:
    """Projects close while the pages are read: nine in ten is enough."""
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 10)], total=10, pages=1)
        )

    assert len(WantedlyScraper(SLUG).fetch()) == 9


def test_more_pages_than_the_site_serves_is_refused(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API, json=_listing([_project(1)], total=1500, pages=150)
    )

    with pytest.raises(ScraperError, match="only 100 pages can be read"):
        WantedlyScraper(SLUG).fetch()


def test_projects_of_several_companies_are_refused(httpx_mock) -> None:
    other = _project(2, company={"id": 999, "name": "Other"})
    httpx_mock.add_response(url=API, json=_listing([_project(1), other]))

    with pytest.raises(ScraperError, match="projects of 2 companies"):
        WantedlyScraper(SLUG).fetch()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": [], "_metadata": None},
        {"data": [], "_metadata": {"total_objects": "3", "total_pages": 1}},
        {"data": [], "_metadata": {"total_objects": 3, "total_pages": True}},
        {"data": None, "_metadata": {"total_objects": 0, "total_pages": 0}},
        {"data": ["x"], "_metadata": {"total_objects": 1, "total_pages": 1}},
    ],
)
def test_malformed_listing_is_refused(httpx_mock, payload: object) -> None:
    httpx_mock.add_response(url=API, json=payload)

    with pytest.raises(ScraperError):
        WantedlyScraper(SLUG).fetch()


@pytest.mark.parametrize(
    "overrides",
    [{"id": None}, {"id": True}, {"id": "abc"}, {"id": 0}, {"title": "  "}, {"title": None}],
)
def test_project_without_id_or_title_is_refused(httpx_mock, overrides: dict) -> None:
    httpx_mock.add_response(url=API, json=_listing([_project(1, **overrides)]))

    with pytest.raises(ScraperError, match="without"):
        WantedlyScraper(SLUG).fetch()


def test_unknown_company_is_an_error(monkeypatch, httpx_mock) -> None:
    """The site answers 500 for a slug it does not know."""
    import ats_scrapers.fetch

    monkeypatch.setattr(ats_scrapers.fetch, "DEFAULT_RETRIES", 1)
    httpx_mock.add_response(url=API, status_code=500)

    with pytest.raises(ScraperError):
        WantedlyScraper(SLUG).fetch()


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("Acme_Japan", "Acme_Japan"),
        (" wantedly ", "wantedly"),
        ("01GROWTH", "01GROWTH"),
        ("https://www.wantedly.com/companies/Acme_Japan", "Acme_Japan"),
        ("https://www.wantedly.com/companies/Acme_Japan/projects", "Acme_Japan"),
        ("wantedly.com/companies/a-b", "a-b"),
    ],
)
def test_accepts_a_slug_or_a_company_address(value: str, slug: str) -> None:
    assert WantedlyScraper(value).company_slug == slug


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "acme japan",
        "acme?x",
        "_acme",
        "a" * 65,
        "https://www.wantedly.com/projects/123",
        "https://evil.example/companies/acme",
        "http://www.wantedly.com/companies/acme",
        "https://www.wantedly.com/companies/",
    ],
)
def test_rejects_other_values(value: str) -> None:
    with pytest.raises(ScraperError, match="WantedlyScraper"):
        WantedlyScraper(value)


def test_missing_optional_fields_stay_empty(httpx_mock) -> None:
    bare = _project(1, location=None, location_suffix="", description=None, published_at="soon")
    httpx_mock.add_response(url=API, json=_listing([bare]))

    job = WantedlyScraper(SLUG).fetch()[0]

    assert job.location is None
    assert job.description is None
    assert job.posted_at is None


# --- One opening republished as several projects ----------------------------

LONG_TEXT = "家づくりを検討するお客様と住宅会社をつなぐ事業で、社内のシステムを一から整えます。" * 6


def _repost(project_id: int, **overrides: object) -> dict[str, object]:
    """A project whose text is long enough to recognise a republished opening."""
    fields: dict[str, object] = {
        "description": LONG_TEXT,
        "looking_for": "社内SE・1人目社員",
        "location": "愛知県名古屋市",
        "location_suffix": None,
    }
    fields.update(overrides)
    return _project(project_id, **fields)


def test_republished_projects_are_one_job(httpx_mock) -> None:
    """Same role label and same text under three headlines, two prefectures:
    one job, the most recent project, with every place and the other ids."""
    httpx_mock.add_response(
        url=API,
        json=_listing(
            [
                _repost(10, title="代表直下の社内SE", published_at="2026-08-01T10:00:00+09:00"),
                _repost(
                    30,
                    title="1人目情シス",
                    published_at="2026-10-30T10:00:00+09:00",
                    location="岐阜県",
                ),
                _repost(20, title="社内のシステム構築", published_at="2026-09-15T10:00:00+09:00"),
            ]
        ),
    )

    (job,) = WantedlyScraper(SLUG).fetch()

    assert job.ats_id == "30"
    assert job.title == "1人目情シス"
    assert str(job.url) == "https://www.wantedly.com/projects/30"
    assert job.posted_at == datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
    assert job.location == "岐阜県; 愛知県名古屋市"
    assert job.raw == {"company_slug": SLUG, "reposts": 2, "repost_ids": ["20", "10"]}


def test_a_single_project_carries_no_repost_fields(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_repost(1)]))

    (job,) = WantedlyScraper(SLUG).fetch()

    assert job.location == "愛知県名古屋市"
    assert job.raw == {"company_slug": SLUG}


@pytest.mark.parametrize(
    "other",
    [
        {"description": LONG_TEXT + "勤務地は岐阜です。"},
        {"looking_for": "フルスタックマーケター"},
        {"looking_for": None},
    ],
    ids=["another-text", "another-role", "no-role"],
)
def test_projects_that_differ_stay_apart(httpx_mock, other: dict[str, object]) -> None:
    httpx_mock.add_response(url=API, json=_listing([_repost(1), _repost(2, **other)]))

    jobs = WantedlyScraper(SLUG).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]
    assert all(job.raw == {"company_slug": SLUG} for job in jobs)


@pytest.mark.parametrize(
    "text",
    ["詳細は面談でお話しします。", "", None, LONG_TEXT[:199]],
    ids=["short", "empty", "missing", "just-under-the-limit"],
)
def test_a_short_or_missing_text_never_joins_projects(httpx_mock, text: object) -> None:
    httpx_mock.add_response(
        url=API,
        json=_listing([_repost(1, description=text), _repost(2, description=text)]),
    )

    assert [job.ats_id for job in WantedlyScraper(SLUG).fetch()] == ["1", "2"]


def test_republished_projects_can_be_kept_apart(httpx_mock) -> None:
    httpx_mock.add_response(url=API, json=_listing([_repost(1), _repost(2)]))

    jobs = WantedlyScraper(SLUG, collapse_reposts=False).fetch()

    assert [job.ats_id for job in jobs] == ["1", "2"]
    assert all(job.raw == {"company_slug": SLUG} for job in jobs)


def test_republished_projects_are_joined_without_their_text(httpx_mock) -> None:
    """``include_descriptions=False`` drops the text from the job, not from
    the comparison: the listing carries it either way."""
    httpx_mock.add_response(url=API, json=_listing([_repost(1), _repost(2)]))

    (job,) = WantedlyScraper(SLUG, include_descriptions=False).fetch()

    assert job.description is None
    assert job.raw == {"company_slug": SLUG, "reposts": 1, "repost_ids": ["1"]}


def test_whitespace_differences_do_not_split_an_opening(httpx_mock) -> None:
    spaced = LONG_TEXT.replace("。", "。\r\n\r\n ")
    httpx_mock.add_response(
        url=API,
        json=_listing(
            [_repost(1), _repost(2, description=spaced, looking_for=" 社内SE・1人目社員 ")]
        ),
    )

    assert len(WantedlyScraper(SLUG).fetch()) == 1


def test_jobs_keep_the_order_their_opening_was_first_listed(httpx_mock) -> None:
    httpx_mock.add_response(
        url=API,
        json=_listing(
            [
                _repost(5, published_at="2026-01-01T00:00:00+09:00"),
                _project(7),
                _repost(9, published_at="2026-02-01T00:00:00+09:00"),
                _project(3),
            ]
        ),
    )

    assert [job.ats_id for job in WantedlyScraper(SLUG).fetch()] == ["9", "7", "3"]


def test_only_the_first_repost_ids_are_kept(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.wantedly as wantedly

    monkeypatch.setattr(wantedly, "MAX_REPOST_IDS", 2)
    httpx_mock.add_response(
        url=API, json=_listing([_repost(project_id) for project_id in (1, 2, 3, 4)])
    )

    (job,) = WantedlyScraper(SLUG).fetch()

    assert job.ats_id == "4"
    assert job.raw == {"company_slug": SLUG, "reposts": 3, "repost_ids": ["3", "2"]}
