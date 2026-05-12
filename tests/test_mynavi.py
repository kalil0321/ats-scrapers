"""Tests for the MyNavi Tenshoku (Japan mid-career) scraper.

MyNavi has no public JSON API — listings are SSR'd HTML. These tests
pin the card-parsing contract against real-shape HTML fixtures plus
the ``/list/pg{n}/`` pagination walk.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import MyNaviScraper, ScraperRegistry, get_scraper

# All page fetches go to ``tenshoku.mynavi.jp/list...`` — match anything
# under that prefix so httpx_mock matches both ``/list/`` and ``/list/pgN/``.
_LISTING_URL_RE = re.compile(r"^https://tenshoku\.mynavi\.jp/list/")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.mynavi as m
    monkeypatch.setattr(m, "MAX_RETRIES", 1)
    monkeypatch.setattr(m, "RETRY_BASE_DELAY", 0.0)


# ---------------------------------------------------------------------------
# Fixture HTML — mirrors the production ``cassetteRecruit`` markup. Anything
# not exercised by the tests is trimmed to keep the fixture readable, but
# the wrapper / class names match MyNavi's live output verbatim so a
# selector regression on either side surfaces here.
# ---------------------------------------------------------------------------


def _card(
    *,
    variant: str = "",  # "" = cassetteRecruit, "Recommend" = cassetteRecruitRecommend
    job_key: str = "2775727",
    jobinfo_slug: str = "jobinfo-188316-1-108",
    href_suffix: str = "/",
    title: str = "未経験OK！【キャリアコーディネーター】土日祝休／年間124日休",
    company: str = "株式会社トライトキャリア | 業界大手の成長企業",
    employment: str | None = "正社員",
    location: str | None = "【転勤なし】 東京・大阪・名古屋・愛知 ほか",
    job_content: str | None = "人材を求める企業と派遣先候補をつなぐマッチング業務",
    target: str | None = "学歴・性別不問／第二新卒も歓迎",
    salary_free: str | None = "月給28万円〜＋インセンティブ",
    salary_first_year: str | None = "400万円～500万円",
    update_date: str | None = "2026/04/21",
    feature_tags: list[str] | None = None,
) -> str:
    cls_root = f"cassetteRecruit{variant}"
    if feature_tags is None:
        feature_tags = [
            "職種・業種未経験OK", "転勤なし", "学歴不問",
            "完全週休2日制", "第二新卒歓迎",
        ]
    href = f"//tenshoku.mynavi.jp/{jobinfo_slug}-1{href_suffix}"
    emp_html = (
        f'<span class="labelEmploymentStatus">{employment}</span>'
        if employment else ""
    )
    rows: list[tuple[str, str | None]] = [
        ("仕事内容", job_content),
        ("対象となる方", target),
        ("勤務地", location),
        ("給与", salary_free),
        ("初年度年収", salary_first_year),
    ]
    table_rows = "".join(
        f'<tr><th class="tableCondition__head">{h}</th>'
        f'<td class="tableCondition__body">{v}</td></tr>'
        for h, v in rows if v is not None
    )
    feature_html = "".join(
        f'<li class="{cls_root}__attributeLabel">'
        f'<span class="labelCondition">{t}</span></li>'
        for t in feature_tags
    )
    update_html = (
        f'<p class="{cls_root}__updateDate">情報更新日：'
        f'<span>{update_date}</span></p>'
        if update_date else ""
    )
    return (
        f'<div class="{cls_root}">'
        f'<div class="{cls_root}__content js__link--post" '
        f'data-ty="fnc_sr" data-show-no="1">'
        f'<section class="{cls_root}__heading cassetteUseFloat">'
        f'<h3 class="{cls_root}__name">{company}</h3>'
        f'<p class="{cls_root}__copy boxAdjust">'
        f'<a class="js__ga--setCookieOccName" target="_blank" '
        f'href="{href}">{title}</a>'
        f'{emp_html}'
        f'</p>'
        f'</section>'
        f'<div class="{cls_root}__detail">'
        f'<ul class="{cls_root}__attribute">{feature_html}</ul>'
        f'<div class="{cls_root}__main">'
        f'<table class="tableCondition"><tbody>{table_rows}</tbody></table>'
        f'</div>'
        f'</div>'
        f'<div class="{cls_root}__bottom">'
        f'<button class="btnInterst" data-job-key="{job_key}">気になる</button>'
        f'{update_html}'
        f'</div>'
        f'</div></div>'
    )


def _page(cards: list[str], *, total: int = 100) -> str:
    """Wrap cards in the minimum surrounding HTML the parser inspects."""
    body = "".join(cards) if cards else ""
    return (
        '<html><body>'
        '<div class="container__inner">'
        '<div class="result"><div class="result__info">'
        f'<p class="result__num"><em>{total}</em><span>件</span></p>'
        '</div></div>'
        f'{body}'
        '<nav class="pager"><ul class="pager__list js__pageSubmit">'
        '<li class="pager__item--active"><a>1</a></li>'
        '</ul></nav>'
        '</body></html>'
    )


def _empty_page() -> str:
    """The "page not found" template MyNavi serves for out-of-range pages."""
    return (
        '<html><body>'
        '<h1>お探しのページは見つかりませんでした。</h1>'
        '</body></html>'
    )


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_mynavi() -> None:
    assert ScraperRegistry.get(ATSType.MYNAVI) is MyNaviScraper


def test_get_scraper_by_string_returns_mynavi() -> None:
    s = get_scraper("mynavi", "any")
    assert isinstance(s, MyNaviScraper)


# --- happy-path parsing -----------------------------------------------------


def test_parses_full_cassette_card(httpx_mock) -> None:
    """Single ``cassetteRecruit`` card — pin every field that maps from
    a primary HTML element to a ``Job`` attribute."""
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_page([_card()]))
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())

    jobs = MyNaviScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.MYNAVI
    assert j.ats_id == "2775727"
    assert j.title.startswith("未経験OK")
    assert "トライトキャリア" in j.company
    assert str(j.url) == "https://tenshoku.mynavi.jp/jobinfo-188316-1-108-1/"
    assert j.country_iso == "JP"
    assert j.language == "ja"
    assert j.location is not None and "転勤なし" in j.location
    assert j.posted_at is not None
    assert j.posted_at.year == 2026 and j.posted_at.month == 4


# --- salary parsing ---------------------------------------------------------


def test_parses_first_year_salary_range_in_yen(httpx_mock) -> None:
    """``初年度年収`` is the structured first-year income field — units
    are 万 (10,000 JPY). ``400万円～500万円`` ⇒ 4_000_000 / 5_000_000 JPY."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(salary_first_year="400万円～500万円")]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.salary_currency == "JPY"
    assert j.salary_period == "YEAR"
    assert j.salary_summary == "400万円～500万円"
    assert j.salary_min == 4_000_000
    assert j.salary_max == 5_000_000


def test_accepts_wide_and_narrow_tilde_in_salary(httpx_mock) -> None:
    """MyNavi inconsistently uses ``～`` (FULLWIDTH) and ``〜`` (WAVE
    DASH). Both must parse."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([
            _card(job_key="1", salary_first_year="300万円〜600万円"),
            _card(job_key="2", salary_first_year="300万円～600万円"),
        ]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    jobs = MyNaviScraper("any").fetch()
    assert {j.salary_min for j in jobs} == {3_000_000}
    assert {j.salary_max for j in jobs} == {6_000_000}


def test_no_salary_when_first_year_field_missing(httpx_mock) -> None:
    """When ``初年度年収`` is absent and only the free-text ``給与`` field
    is present (often non-yen for overseas postings), we don't invent
    a range. ``salary_summary`` falls back to the free text but
    ``salary_min``/``salary_max`` stay ``None``."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(
            salary_first_year=None,
            salary_free="月給5万バーツ〜8万バーツ",
        )]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.salary_summary == "月給5万バーツ〜8万バーツ"
    assert j.salary_min is None
    assert j.salary_max is None
    # currency stays None when we can't parse a numeric range — we don't
    # want a JPY label sitting on a Thai-baht posting.
    assert j.salary_currency is None


def test_free_text_salary_stashed_in_raw_when_distinct(httpx_mock) -> None:
    """The free-text 給与 (monthly + bonuses) is useful context even
    when we use the structured first-year range, so it lives in
    ``raw.salary_free_text``."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(
            salary_free="月給25万円〜34万円＋賞与5.5カ月",
            salary_first_year="400万円～950万円",
        )]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw.get("salary_free_text") == "月給25万円〜34万円＋賞与5.5カ月"


# --- employment-type mapping ------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("正社員", "FULL_TIME"),
        ("契約社員", "CONTRACT"),
        ("派遣社員", "CONTRACT"),
        ("業務委託", "CONTRACT"),
        ("アルバイト・パート", "PART_TIME"),
        ("インターン", "INTERN"),
    ],
)
def test_employment_label_maps_to_canonical_type(
    label: str, expected: str, httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(employment=label)]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.employment_type == expected
    # Raw Japanese label survives on ``commitment`` for downstream
    # consumers that want the original granularity.
    assert j.commitment == label


def test_unknown_employment_label_preserved_in_raw(httpx_mock) -> None:
    """If MyNavi adds a new label we don't have a mapping for, the
    canonical ``employment_type`` stays ``None`` but the raw label is
    kept in ``raw.employment_label_raw`` so we don't lose the signal."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(employment="顧問")]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.employment_type is None
    assert j.commitment == "顧問"
    assert j.raw is not None
    assert j.raw.get("employment_label_raw") == "顧問"


# --- card variants ----------------------------------------------------------


def test_cassette_recruit_recommend_variant_is_parsed(httpx_mock) -> None:
    """MyNavi mixes regular ``cassetteRecruit`` cards with ``Recommend``
    sponsored variants (notice the suffixed CSS classes). Both share
    the same inner schema and must both parse."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([
            _card(variant="", job_key="100"),
            _card(variant="Recommend", job_key="200"),
        ]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    jobs = MyNaviScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


# --- feature tags / raw -----------------------------------------------------


def test_feature_tags_collected_into_raw(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(
            feature_tags=["リモートワーク可", "上場", "学歴不問"]
        )]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.raw is not None
    assert j.raw["feature_tags"] == ["リモートワーク可", "上場", "学歴不問"]
    assert j.raw["jobinfo_slug"] == "jobinfo-188316-1-108"


# --- description ------------------------------------------------------------


def test_description_concatenates_job_content_and_target(httpx_mock) -> None:
    """The listing card surfaces two prose teasers (仕事内容 / 対象となる方);
    we join them as ``description`` because the detail page (where the
    full description lives) is out of scope for this scraper."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(
            job_content="エンジニア業務",
            target="未経験歓迎",
        )]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    j = MyNaviScraper("any").fetch()[0]
    assert j.description == "エンジニア業務\n\n未経験歓迎"


# --- pagination -------------------------------------------------------------


def test_walks_pagination_until_empty_page(httpx_mock) -> None:
    """The pagination cursor is path-based (``/list/pgN/``); the walk
    terminates when a page returns the "page not found" template."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="1"), _card(job_key="2")]),
    )
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="3")]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    jobs = MyNaviScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_dedupes_overlap_between_pages(httpx_mock) -> None:
    """Defensive: when the same card surfaces on two consecutive pages
    (server-side ordering jitter against a moving total), keep only
    the first copy."""
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="1"), _card(job_key="2")]),
    )
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        # Page 2 repeats key=2 then has a fresh key=3.
        html=_page([_card(job_key="2"), _card(job_key="3")]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    jobs = MyNaviScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]


def test_max_pages_caps_the_walk(httpx_mock) -> None:
    """``max_pages`` is the smoke-test ceiling; the scraper must stop
    even when every page keeps returning fresh cards."""
    # Three pages of fresh cards — but max_pages=2, so the third never
    # gets requested. Use ``is_reusable`` because httpx_mock can serve
    # the same response twice.
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="1")]),
    )
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="2")]),
    )
    jobs = MyNaviScraper("any", max_pages=2).fetch()
    assert {j.ats_id for j in jobs} == {"1", "2"}


# --- defensive --------------------------------------------------------------


def test_skips_card_missing_required_fields(httpx_mock) -> None:
    """Cards without a title or company should be dropped — don't emit
    half-built rows."""
    # Build a malformed card that has the wrapper but no title.
    malformed = (
        '<div class="cassetteRecruit">'
        '<div class="cassetteRecruit__content js__link--post">'
        '<h3 class="cassetteRecruit__name">Only Co</h3>'
        '<button data-job-key="99">x</button>'
        '</div></div>'
    )
    httpx_mock.add_response(
        url=_LISTING_URL_RE,
        html=_page([_card(job_key="1"), malformed]),
    )
    httpx_mock.add_response(url=_LISTING_URL_RE, html=_empty_page())
    jobs = MyNaviScraper("any").fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL_RE, status_code=500, is_reusable=True,
    )
    with pytest.raises(ScraperError):
        MyNaviScraper("any").fetch()
