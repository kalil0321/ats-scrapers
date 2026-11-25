from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
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

    assert _asked(httpx_mock) == [
        {"company_id": SLUG, "type": "recent", "per_page": "10", "page": "1"}
    ]
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


def test_pages_are_read_again_when_a_pass_skips_a_project(httpx_mock) -> None:
    """A project published during the read shifts the pages: project 10
    shows on both pages of the first pass and project 11 on none."""
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


COMPANY = f"https://www.wantedly.com/companies/{SLUG}"


def _company(project_count: int, slug: str = SLUG) -> dict[str, object]:
    return {"company": {"id": 180819, "company_name": slug, "project_count": project_count}}


def test_list_far_short_of_the_announced_total_is_refused(httpx_mock) -> None:
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 11)], total=40, pages=1)
        )
    httpx_mock.add_response(url=COMPANY, json=_company(40))

    with pytest.raises(ScraperError, match="announces 40 projects and listed 10"):
        WantedlyScraper(SLUG).fetch()


def test_projects_the_listing_counts_but_never_lists_are_left_out(httpx_mock) -> None:
    """The listing counts 15 projects and lists 12 on its two pages; the
    company page shows 12."""
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 11)], total=15)
        )
        httpx_mock.add_response(
            url=API, json=_listing([_project(11), _project(12)], total=15)
        )
    httpx_mock.add_response(url=COMPANY, json=_company(12))

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 13))


def test_company_page_without_projects_gives_an_empty_list(httpx_mock) -> None:
    """The listing counts a project shared by link only and lists nothing."""
    for _ in range(4):
        httpx_mock.add_response(url=API, json=_listing([], total=1, pages=1))
    httpx_mock.add_response(url=COMPANY, json=_company(0))

    assert WantedlyScraper(SLUG).fetch() == []


@pytest.mark.parametrize(
    "response",
    [
        {"json": _company(10, slug="Other_Company")},
        {"json": {"company": {"company_name": SLUG, "project_count": "10"}}},
        {"status_code": 404},
    ],
)
def test_company_page_that_does_not_tell_keeps_the_refusal(httpx_mock, response) -> None:
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 11)], total=40, pages=1)
        )
    httpx_mock.add_response(url=COMPANY, **response)

    with pytest.raises(ScraperError, match="announces 40 projects and listed 10"):
        WantedlyScraper(SLUG).fetch()


def test_list_close_to_the_announced_total_is_kept(httpx_mock) -> None:
    """Projects close while the pages are read: nine in ten is enough."""
    for _ in range(4):
        httpx_mock.add_response(
            url=API, json=_listing([_project(i) for i in range(1, 10)], total=10, pages=1)
        )

    assert len(WantedlyScraper(SLUG).fetch()) == 9


# --- Companies past the 100-page cap ----------------------------------------

FILTERS = ("occupation_types[]", "hiring_types[]", "locations[]")


def _serve(
    httpx_mock,
    projects: list[tuple[int, dict[str, set[str]]]],
    after: Callable[[dict[str, str]], None] | None = None,
) -> None:
    """The listing as the site serves it: each filter keeps the projects
    tagged with its value, ten a page, and nothing past page 100.

    ``after`` runs once a request is answered and may change ``projects``,
    newest first, as a company publishing or closing projects would."""

    def reply(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        pool = [
            project_id
            for project_id, tags in projects
            if all(params[key] in tags.get(key, set()) for key in FILTERS if key in params)
        ]
        page = int(params["page"])
        shown = pool[(page - 1) * 10 : page * 10] if page <= 100 else []
        if after is not None:
            after(dict(params))
        return httpx.Response(
            200, json=_listing([_project(i) for i in shown], total=len(pool))
        )

    httpx_mock.add_callback(reply, url=API, is_reusable=True)


def _tags(occupation: str, hiring: str = "mid_career", *areas: str) -> dict[str, set[str]]:
    tags = {"hiring_types[]": {hiring}, "locations[]": set(areas or ("tokyo", "kanto"))}
    if occupation:
        tags["occupation_types[]"] = {occupation}
    return tags


def _narrowed(httpx_mock) -> set[tuple[str, ...]]:
    return {
        tuple(f"{key}={value}" for key, value in params.items() if key in FILTERS)
        for params in _asked(httpx_mock)
    }


def test_company_past_the_page_cap_is_read_one_occupation_at_a_time(httpx_mock) -> None:
    projects = [(i, _tags("jp__engineering")) for i in range(1, 601)]
    projects += [(i, _tags("jp__sales")) for i in range(601, 1006)]
    _serve(httpx_mock, projects)

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 1006))
    asked = _asked(httpx_mock)
    assert max(int(params["page"]) for params in asked) == 60
    assert all(params["type"] == "recent" for params in asked)
    assert ("occupation_types[]=jp__sales",) in _narrowed(httpx_mock)
    assert not any(len(filters) > 1 for filters in _narrowed(httpx_mock))


def test_occupation_past_the_page_cap_is_split_by_hiring_type(httpx_mock) -> None:
    projects = [(i, _tags("jp__engineering", "mid_career")) for i in range(1, 701)]
    projects += [(i, _tags("jp__engineering", "freelance")) for i in range(701, 1201)]
    _serve(httpx_mock, projects)

    jobs = WantedlyScraper(SLUG).fetch()

    assert len({job.ats_id for job in jobs}) == 1200
    assert (
        "occupation_types[]=jp__engineering",
        "hiring_types[]=freelance",
    ) in _narrowed(httpx_mock)


def test_areas_are_split_by_region(httpx_mock) -> None:
    """``tokyo`` lies in ``kanto``: only the regions, which do not overlap,
    split a listing."""
    projects = [(i, _tags("jp__sales", "mid_career", "tokyo", "kanto")) for i in range(1, 601)]
    projects += [(i, _tags("jp__sales", "mid_career", "osaka", "kinki")) for i in range(601, 1101)]
    _serve(httpx_mock, projects)

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 1101))
    areas = {params.get("locations[]") for params in _asked(httpx_mock)}
    assert {"kanto", "kinki"} <= areas
    assert not areas & {"tokyo", "osaka"}


def test_slice_that_no_filter_brings_under_the_cap_is_refused(httpx_mock) -> None:
    _serve(httpx_mock, [(i, _tags("jp__sales", "mid_career")) for i in range(1, 1011)])

    with pytest.raises(
        ScraperError,
        match=(
            r"occupation_types\[\]=jp__sales, hiring_types\[\]=mid_career, "
            r"locations\[\]=kanto\) announces 1010 projects on 101 pages; "
            "only 100 pages can be read"
        ),
    ):
        WantedlyScraper(SLUG).fetch()


def test_projects_without_an_occupation_are_read_by_hiring_type(httpx_mock) -> None:
    """The occupations hold 1,000 of 1,050 projects, which would pass the
    coverage check: the hiring types, which hold them all, split instead."""
    projects = [(i, _tags("jp__sales", "mid_career")) for i in range(1, 601)]
    projects += [(i, _tags("jp__engineering", "freelance")) for i in range(601, 1001)]
    projects += [(i, _tags("", "freelance")) for i in range(1001, 1051)]
    _serve(httpx_mock, projects)

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 1051))
    assert ("hiring_types[]=freelance",) in _narrowed(httpx_mock)


def test_projects_no_filter_reaches_are_refused(httpx_mock) -> None:
    projects = [(i, _tags("jp__sales")) for i in range(1, 1001)]
    projects += [(i, _tags("")) for i in range(1001, 1051)]
    _serve(httpx_mock, projects)

    with pytest.raises(
        ScraperError,
        match=(
            r"hiring_types\[\]=mid_career, locations\[\]=kanto\) announces 1050 projects "
            "on 105 pages; only 100 pages can be read, and no filter splits them"
        ),
    ):
        WantedlyScraper(SLUG).fetch()


def test_project_closed_during_the_read_does_not_fail_the_split(httpx_mock) -> None:
    """A sales project closes after the listing announced 1,050 and before
    the sales slice is read: the slices hold 1,049, as the listing now does."""
    projects = [(i, _tags("jp__sales")) for i in range(1, 601)]
    projects += [(i, _tags("jp__engineering")) for i in range(601, 1051)]

    def close_one(params: dict[str, str]) -> None:
        if params.get("occupation_types[]") == "jp__engineering" and params["page"] == "1":
            projects[:] = [project for project in projects if project[0] != 7]

    _serve(httpx_mock, projects, after=close_one)

    jobs = WantedlyScraper(SLUG).fetch()

    assert len(jobs) == 1049
    assert not any("hiring_types[]" in params for params in _asked(httpx_mock))


def test_coverage_is_measured_on_the_total_after_projects_closed(httpx_mock) -> None:
    """1,100 announced, 200 closed during the read, 900 read: the read is
    complete, though 900 is under 90% of the first total."""
    projects = [(i, _tags("jp__sales")) for i in range(1, 601)]
    projects += [(i, _tags("jp__engineering")) for i in range(601, 1101)]

    def close_some(params: dict[str, str]) -> None:
        if params.get("occupation_types[]") == "jp__engineering" and params["page"] == "1":
            projects[:] = [project for project in projects if project[0] > 200]

    _serve(httpx_mock, projects, after=close_some)

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(201, 1101))


def test_listing_that_grows_past_the_cap_on_a_later_pass_is_split(httpx_mock) -> None:
    """Ten projects published while page 50 is read shift the pages, so the
    first pass falls short; the second finds 101 pages and must not stop at
    page 100, past which ten projects would stay out of reach."""
    projects = [(i, _tags("jp__sales" if i % 2 else "jp__engineering")) for i in range(1, 1001)]

    def publish(params: dict[str, str]) -> None:
        if params["page"] == "50" and len(projects) == 1000:
            projects[:0] = [(i, _tags("jp__sales")) for i in range(1001, 1011)]

    _serve(httpx_mock, projects, after=publish)

    jobs = WantedlyScraper(SLUG).fetch()

    assert sorted(int(job.ats_id) for job in jobs) == list(range(1, 1011))
    assert ("occupation_types[]=jp__sales",) in _narrowed(httpx_mock)


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
