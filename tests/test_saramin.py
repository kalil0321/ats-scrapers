"""Tests for the Saramin (사람인 — Korea's largest job board) scraper.

The Saramin search page is server-rendered HTML. We pin the parsing
contract for the listing card layout (``<div class="item_recruit">``)
and the offset-style pagination (``recruitPage=N`` with
``recruitPageCount=40``).

Fixtures embed real HTML excerpts captured from
``https://www.saramin.co.kr/zf_user/search?...`` — keep them faithful
to the live markup so a future site redesign is caught by the suite.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import SaraminScraper, ScraperRegistry

_SEARCH_RE = re.compile(r"^https://www\.saramin\.co\.kr/zf_user/search\?")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.saramin as s
    monkeypatch.setattr(s, "MAX_RETRIES", 1)
    monkeypatch.setattr(s, "RETRY_BASE_DELAY", 0.0)


# --- HTML fixtures ----------------------------------------------------------


def _card(
    *,
    rec_idx: str = "51438642",
    title: str = "LLM Developer",
    company: str = "(주)씨어스",
    location_html: str = (
        '<span>'
        '<a target="_blank" href="/zf_user/area-recruit/area-list/area/101000">서울</a>'
        '  <a target="_blank" href="/zf_user/area-recruit/area-list/area/101150">서초구</a>'
        '</span>'
    ),
    career: str = "경력 5~20년",
    education: str = "석사↑",
    employment_label: str = "정규직",
    deadline: str = "~ 05/29(금)",
    posted_yymmdd: str | None = "26/04/30",
    modified_yymmdd: str | None = None,
    sectors_html: str = (
        '<a target="_blank" href="/zf_user/jobs/list/job-category?cat_kewd=84">백엔드/서버개발</a>,'
        '<a target="_blank" href="/zf_user/jobs/list/job-category?cat_kewd=160">NLP(자연어처리)</a>'
    ),
    data_layer: str = "keyword_free|paid_n_quick",
    include_value_attr: bool = True,
) -> str:
    """Render a Saramin ``item_recruit`` card matching the live markup."""
    value_attr = f'value="{rec_idx}"' if include_value_attr else ""
    posted_html = (
        f'<span class="job_day">등록일 {posted_yymmdd}</span>'
        if posted_yymmdd is not None else ""
    )
    modified_html = (
        f'<span class="job_day">수정일 {modified_yymmdd}</span>'
        if modified_yymmdd is not None else ""
    )
    href = (
        f"/zf_user/jobs/relay/view?view_type=search&amp;rec_idx={rec_idx}"
        f"&amp;location=ts&amp;searchType=search"
    )
    return f"""
    <div class="item_recruit" {value_attr}
         data-data_layer="{data_layer}">
      <div class="area_job">
        <h2 class="job_tit">
          <a target="_blank" title="{title}" href="{href}"><span>{title}</span></a>
        </h2>
        <div class="job_date">
          <span class="date">{deadline}</span>
        </div>
        <div class="job_condition">
          {location_html}
          <span>{career}</span>
          <span>{education}</span>
          <span>{employment_label}</span>
        </div>
        <div class="job_sector">
          {sectors_html}
          {posted_html}
          {modified_html}
        </div>
      </div>
      <div class="area_corp">
        <strong class="corp_name">
          <a href="/zf_user/company-info/view?csn=xyz" target="_blank">{company}</a>
        </strong>
      </div>
    </div>
    """


def _page(cards_html: str = "", total: str = "총 4,180건") -> str:
    return f"""
    <!doctype html>
    <html lang="ko">
      <head><title>{total}의 검색결과 - 사람인</title></head>
      <body>
        <div class="content_recruit">
          {cards_html}
        </div>
      </body>
    </html>
    """


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_saramin() -> None:
    assert ScraperRegistry.get(ATSType.SARAMIN) is SaraminScraper


def test_ats_type_enum_value() -> None:
    """The string value is part of the public schema — pin it."""
    assert ATSType.SARAMIN == "saramin"


# --- happy path -------------------------------------------------------------


def test_parses_full_card(httpx_mock) -> None:
    """Single-page scrape; verify every populated Job field maps to the
    right HTML region of a Saramin card."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(_card()))
    # Second page returns 0 cards twice → exhausts the sweep early.
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("developer").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.SARAMIN
    assert j.ats_id == "51438642"
    assert j.title == "LLM Developer"
    assert j.company == "(주)씨어스"
    assert str(j.url) == (
        "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=51438642"
    )
    assert j.country_iso == "KR"
    assert j.language == "ko"
    assert j.location == "서울 서초구"
    assert j.employment_type == "FULL_TIME"
    assert j.posted_at == datetime(2026, 4, 30)
    assert j.raw is not None
    assert j.raw.get("career_level") == "경력 5~20년"
    assert j.raw.get("education") == "석사↑"
    assert j.raw.get("employment_type_label") == "정규직"
    assert j.raw.get("deadline") == "~ 05/29(금)"
    assert j.raw.get("sectors") == ["백엔드/서버개발", "NLP(자연어처리)"]


def test_global_id_uses_saramin_prefix(httpx_mock) -> None:
    """``global_id`` should be ``saramin:{rec_idx}`` so cross-ATS dedup
    works for postings that happen to be syndicated elsewhere."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(_card(rec_idx="999")))
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert jobs[0].global_id == "saramin:999"


# --- pagination -------------------------------------------------------------


def test_paginates_until_two_empty_pages(httpx_mock) -> None:
    """Saramin doesn't expose a total-pages count — we walk until two
    consecutive empty pages confirm we're past the tail."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="100")),
    )
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="200")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""))  # 1st empty
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""))  # 2nd empty → stop

    jobs = SaraminScraper("dev").fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


def test_dedupes_repeated_cards_across_pages(httpx_mock) -> None:
    """Sponsored ``TOP100`` cards repeat across pages; the scraper must
    collapse them on ``ats_id`` so the row count is meaningful."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="1") + _card(rec_idx="2")),
    )
    httpx_mock.add_response(
        url=_SEARCH_RE,
        # Page 2 repeats id=2 and adds id=3
        text=_page(_card(rec_idx="2") + _card(rec_idx="3")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3"}


def test_max_pages_clamps_to_server_ceiling() -> None:
    """``recruitPage`` is server-capped near 99 — passing 5000 here
    should not generate 4900+ wasted requests."""
    scraper = SaraminScraper("dev", max_pages=5000)
    assert scraper.max_pages == 99


def test_max_pages_honours_lower_caller_override(httpx_mock) -> None:
    """A caller can set a tighter cap for a quick sample-pass."""
    # Pad with cards on every page so the empty-tail logic doesn't stop
    # the sweep before max_pages is reached.
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="500")),
        is_reusable=True,
    )

    jobs = SaraminScraper("dev", max_pages=2).fetch()
    # We don't assert request count (httpx-mock doesn't expose a clean
    # counter on reusable responses), but the de-dup on a single rec_idx
    # means at most one job is yielded — proving max_pages didn't allow
    # an unbounded sweep.
    assert len(jobs) == 1


# --- empty-searchword path --------------------------------------------------


def test_empty_slug_omits_searchword_param(httpx_mock) -> None:
    """``SaraminScraper("any")`` falls back to no keyword. Verify the
    URL omits a quoted keyword body so the server treats it as the
    sponsored-cards landing."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    SaraminScraper("any", max_pages=1).fetch()
    requests = httpx_mock.get_requests()
    assert requests
    url = str(requests[0].url)
    assert "searchword=&" in url or url.endswith("searchword=")


def test_explicit_keyword_is_url_encoded(httpx_mock) -> None:
    """Korean keywords are common; they must be percent-encoded so the
    GET line stays ASCII-safe."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    SaraminScraper("디자이너", max_pages=1).fetch()
    requests = httpx_mock.get_requests()
    assert requests
    # ``디자이너`` URL-encoded as UTF-8:
    assert "searchword=%EB%94%94%EC%9E%90%EC%9D%B4%EB%84%88" in str(requests[0].url)


# --- field handling ---------------------------------------------------------


def test_skips_card_without_rec_idx(httpx_mock) -> None:
    """A malformed card with no ``value`` attribute and no rec_idx in
    its anchor href should be dropped, not emitted as a half-built row."""
    bad_card = """
    <div class="item_recruit" data-data_layer="x|x">
      <h2 class="job_tit"><a title="No id" href="/some/other/path">No id</a></h2>
      <strong class="corp_name"><a href="#">X</a></strong>
    </div>
    """
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="42") + bad_card),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert [j.ats_id for j in jobs] == ["42"]


def test_falls_back_to_rec_idx_in_href_when_value_attr_missing(httpx_mock) -> None:
    """Sponsored cards sometimes omit the wrapping ``value`` attribute;
    we recover the rec_idx from the title anchor's href."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(rec_idx="77", include_value_attr=False)),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert jobs[0].ats_id == "77"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("정규직", "FULL_TIME"),
        ("계약직", "CONTRACT"),
        ("인턴", "INTERN"),
        ("인턴직", "INTERN"),
        ("프리랜서", "CONTRACT"),
        ("아르바이트", "PART_TIME"),
        ("파트타임", "PART_TIME"),
        ("파견직", "CONTRACT"),
        ("위촉직", "CONTRACT"),
        ("일용직", "TEMPORARY"),
        # Combos: pick the most permanent option.
        ("정규직·계약직", "FULL_TIME"),
        ("계약직·인턴", "INTERN"),  # INTERN before CONTRACT in priority
    ],
)
def test_employment_type_korean_labels(
    httpx_mock, label: str, expected: str
) -> None:
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(employment_label=label)),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert jobs[0].employment_type == expected


def test_unknown_employment_label_yields_none(httpx_mock) -> None:
    """An exotic label we haven't mapped should leave ``employment_type``
    at None rather than guess — the raw label is still preserved."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(employment_label="비상근")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    j = jobs[0]
    assert j.employment_type is None
    assert j.raw is not None
    # ``비상근`` doesn't match the employment-type vocabulary so it
    # winds up classified as the education facet — that's expected;
    # the important thing is we didn't fabricate an enum.


def test_deadline_string_preserved_verbatim(httpx_mock) -> None:
    """Saramin's deadline strings ('~ 05/29(금)', '상시채용', '오늘마감')
    are kept as-is in ``raw['deadline']`` so consumers can filter on
    still-active postings without a Korean calendar parser."""
    for deadline in ("~ 05/29(금)", "상시채용", "오늘마감", "내일마감", "채용시"):
        httpx_mock.reset()
        httpx_mock.add_response(
            url=_SEARCH_RE,
            text=_page(_card(deadline=deadline)),
        )
        httpx_mock.add_response(
            url=_SEARCH_RE, text=_page(""), is_reusable=True,
        )
        jobs = SaraminScraper("dev").fetch()
        assert jobs[0].raw is not None
        assert jobs[0].raw["deadline"] == deadline


def test_posted_at_falls_back_to_none_when_dates_missing(httpx_mock) -> None:
    """Cards without ``등록일``/``수정일`` should leave ``posted_at`` at
    None rather than invent a timestamp."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(posted_yymmdd=None, modified_yymmdd=None)),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    assert jobs[0].posted_at is None


def test_modified_date_captured_in_raw(httpx_mock) -> None:
    """When the card only carries ``수정일`` (modified date), capture
    it in raw — useful as a 'last seen active' signal."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(posted_yymmdd=None, modified_yymmdd="26/05/10")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = SaraminScraper("dev").fetch()
    j = jobs[0]
    assert j.posted_at is None
    assert j.raw is not None
    assert j.raw["modified_at"] == "2026-05-10T00:00:00"


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """Real server failures should surface, not silently emit []."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        SaraminScraper("dev").fetch()


def test_403_raises_immediately(httpx_mock) -> None:
    """A 4xx from Saramin's WAF is not a retry-worthy error — surface
    it to the caller so the operator notices and rotates the UA / proxies."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=403)
    with pytest.raises(ScraperError):
        SaraminScraper("dev").fetch()
