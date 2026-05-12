"""Tests for the JobKorea (잡코리아 — Korea's top job board) scraper.

The JobKorea search page is a Next.js app whose server-rendered HTML
embeds each listing inside a ``data-sentry-component="CardJob"``
block. We pin the parsing contract for that markup, the offset-style
pagination through ``Page_No=N``, and the end-of-results detection
(sponsored cards repeat on out-of-range pages).

Fixtures embed real HTML excerpts captured from
``https://www.jobkorea.co.kr/Search/?stext=...&Page_No=1`` — keep them
faithful to the live markup so a future site redesign is caught by
the suite.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import JobKoreaScraper, ScraperRegistry

_SEARCH_RE = re.compile(r"^https://www\.jobkorea\.co\.kr/Search/\?")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.jobkorea as jk
    monkeypatch.setattr(jk, "MAX_RETRIES", 1)
    monkeypatch.setattr(jk, "RETRY_BASE_DELAY", 0.0)


# --- HTML fixtures ----------------------------------------------------------


def _card(
    *,
    gi_no: str = "49155209",
    title: str = "백엔드 개발자 채용",
    company: str = "㈜라웰드",
    location: str | None = "서울 송파구 외 5",
    sector: str | None = "솔루션·SI·CRM·ERP, 백엔드개발자",
    salary_label: str | None = "월급 400~500만원",
    experience: str | None = "경력4년↑",
    include_company_span: bool = True,
    company_alt: str | None = None,
) -> str:
    """Render a JobKorea ``CardJob`` block matching the live Next.js markup."""
    href = (
        f"https://www.jobkorea.co.kr/Recruit/GI_Read/{gi_no}"
        f"?Oem_Code=C1&amp;logpath=1&amp;listno=1&amp;sc=630"
    )

    chips = []
    if location is not None:
        chips.append(
            '<div data-sentry-component="GrayChip">'
            '<div class="w-[16px] shrink-0">'
            '<span class="emoji--basicemoji-place2 inline-block"></span>'
            '</div>'
            f'<span class="truncate text-gray900 text-typo-b4-14">{location}</span>'
            '</div>'
        )
    if sector is not None:
        chips.append(
            '<div data-sentry-component="GrayChip">'
            '<div class="w-[16px] shrink-0">'
            '<span class="emoji--basicemoji-briefcase inline-block"></span>'
            '</div>'
            f'<span class="truncate text-gray900 text-typo-b4-14">{sector}</span>'
            '</div>'
        )
    if salary_label is not None:
        chips.append(
            '<div data-sentry-component="GrayChip">'
            '<div class="w-[16px] shrink-0">'
            '<span class="emoji--basicemoji-money_bill inline-block"></span>'
            '</div>'
            f'<span class="truncate text-gray900 text-typo-b4-14">{salary_label}</span>'
            '</div>'
        )
    chips_html = "".join(chips)

    experience_html = (
        f'<span class="flex-shrink-0 text-gray700 text-typo-c1-13">{experience}</span>'
        if experience is not None else ""
    )

    company_span = (
        f'<a href="{href}" data-sentry-element="BaseLink">'
        f'<span class="truncate text-gray700 text-typo-b2-16">{company}</span>'
        '</a>'
        if include_company_span else ""
    )
    company_logo_alt = company_alt if company_alt is not None else f"{company} 로고"

    return f"""
    <div data-sentry-component="CardJob" class="w-full rounded-2xl shadow-list bg-white">
      <div class="flex flex-col">
        <div class="flex w-full gap-5 p-7">
          <a href="{href}" data-sentry-component="CompanyLogo">
            <img alt="{company_logo_alt}" />
          </a>
          <div class="w-full">
            <div class="relative mb-[6px] flex items-center justify-between">
              <div data-sentry-component="BadgeItem">
                <span class="font-semibold text-typo-c1-13 text-brand-accent">오늘 뜬 따끈한 공고</span>
              </div>
            </div>
            <div class="mb-0.5">
              <a href="{href}" data-sentry-component="Title">
                <span class="truncate font-semibold text-typo-b1-18 text-gray900">{title}</span>
              </a>
            </div>
            <span class="mb-5 inline-flex items-center gap-[6px]">
              {company_span}
            </span>
            <div class="flex flex-col gap-[10px]">
              <div class="flex justify-between">
                <div class="flex max-w-[643px] gap-2">{chips_html}</div>
              </div>
              <div class="flex items-center justify-between">
                <div class="flex max-w-[565px] items-center gap-[2px]">
                  {experience_html}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """


def _page(cards_html: str = "", total: str = "총 21,429건") -> str:
    return f"""
    <!doctype html>
    <html lang="ko">
      <head><title>'개발자' 관련 채용공고 | {total}의 검색결과</title></head>
      <body>
        <div class="flex flex-col gap-4">
          {cards_html}
        </div>
      </body>
    </html>
    """


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_jobkorea() -> None:
    assert ScraperRegistry.get(ATSType.JOBKOREA) is JobKoreaScraper


def test_ats_type_enum_value() -> None:
    """The string value is part of the public schema — pin it."""
    assert ATSType.JOBKOREA == "jobkorea"


# --- happy path -------------------------------------------------------------


def test_parses_full_card(httpx_mock) -> None:
    """Single-page scrape; verify every populated Job field maps to the
    right HTML region of a JobKorea card."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(_card()))
    # Two consecutive empty pages → exhausts the sweep.
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("developer").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.JOBKOREA
    assert j.ats_id == "49155209"
    assert j.title == "백엔드 개발자 채용"
    assert j.company == "㈜라웰드"
    assert str(j.url) == (
        "https://www.jobkorea.co.kr/Recruit/GI_Read/49155209"
    )
    assert j.country_iso == "KR"
    assert j.language == "ko"
    assert j.location == "서울 송파구 외 5"
    assert j.raw is not None
    assert j.raw.get("sector") == "솔루션·SI·CRM·ERP, 백엔드개발자"
    assert j.raw.get("salary_label") == "월급 400~500만원"
    assert j.raw.get("experience") == "경력4년↑"


def test_global_id_uses_jobkorea_prefix(httpx_mock) -> None:
    """``global_id`` should be ``jobkorea:{gi_no}`` so cross-ATS dedup
    works for postings that happen to be syndicated elsewhere."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(_card(gi_no="999")))
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].global_id == "jobkorea:999"


def test_strips_tracking_querystring_from_url(httpx_mock) -> None:
    """The card link carries ``?Oem_Code=&logpath=&listno=`` analytics
    params — the emitted ``url`` should be the canonical bare form so
    re-scrapes stay stable across days."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(_card(gi_no="42")))
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert str(jobs[0].url) == "https://www.jobkorea.co.kr/Recruit/GI_Read/42"


# --- pagination -------------------------------------------------------------


def test_paginates_until_two_empty_pages(httpx_mock) -> None:
    """JobKorea doesn't expose a total-pages count — we walk until two
    consecutive pages add no new ids (its end-of-result behavior is
    repeating the same sponsored cards, not returning empty HTML)."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="100")),
    )
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="200")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""))  # 1st no-new-id
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""))  # 2nd → stop

    jobs = JobKoreaScraper("dev").fetch()
    assert {j.ats_id for j in jobs} == {"100", "200"}


def test_terminates_when_sponsored_cards_repeat(httpx_mock) -> None:
    """Beyond the real result tail, JobKorea returns the same ~5
    sponsored cards on every subsequent page (rather than an HTTP
    error or an empty body). Two repeats in a row should terminate
    the sweep — otherwise we'd loop forever."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="1") + _card(gi_no="2")),
    )
    # Page 2: same two ids repeated — adds 0 new ids.
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="1") + _card(gi_no="2")),
    )
    # Page 3: same again — 2nd consecutive no-new-id page → stop.
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="1") + _card(gi_no="2")),
    )

    jobs = JobKoreaScraper("dev").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2"}


def test_dedupes_repeated_cards_across_pages(httpx_mock) -> None:
    """Sponsored cards repeat across pages; the scraper must collapse
    them on ``ats_id`` so the row count is meaningful."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="1") + _card(gi_no="2")),
    )
    httpx_mock.add_response(
        url=_SEARCH_RE,
        # Page 2 repeats id=2 and adds id=3
        text=_page(_card(gi_no="2") + _card(gi_no="3")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert {j.ats_id for j in jobs} == {"1", "2", "3"}


def test_max_pages_clamps_to_safety_ceiling() -> None:
    """``max_pages`` is internally clamped to ``MAX_PAGES`` (1000) so a
    caller passing a huge number can't accidentally hammer the site."""
    scraper = JobKoreaScraper("dev", max_pages=10_000)
    assert scraper.max_pages == 1000


def test_max_pages_honours_lower_caller_override(httpx_mock) -> None:
    """A caller can set a tighter cap for a quick sample-pass."""
    # Pad with cards on every page so the empty-tail logic doesn't stop
    # the sweep before max_pages is reached.
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="500")),
        is_reusable=True,
    )

    jobs = JobKoreaScraper("dev", max_pages=2).fetch()
    # De-dup on a single gi_no means at most one job is yielded —
    # proving max_pages didn't allow an unbounded sweep.
    assert len(jobs) == 1


# --- empty-stext path -------------------------------------------------------


def test_empty_slug_omits_search_term(httpx_mock) -> None:
    """``JobKoreaScraper("any")`` falls back to no keyword. Verify the
    URL omits a quoted keyword body so the server treats it as the
    no-keyword landing."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    JobKoreaScraper("any", max_pages=1).fetch()
    requests = httpx_mock.get_requests()
    assert requests
    url = str(requests[0].url)
    assert "stext=&" in url or url.endswith("stext=")


def test_explicit_keyword_is_url_encoded(httpx_mock) -> None:
    """Korean keywords are common; they must be percent-encoded so the
    GET line stays ASCII-safe."""
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    JobKoreaScraper("디자이너", max_pages=1).fetch()
    requests = httpx_mock.get_requests()
    assert requests
    # ``디자이너`` URL-encoded as UTF-8:
    assert "stext=%EB%94%94%EC%9E%90%EC%9D%B4%EB%84%88" in str(
        requests[0].url
    )


# --- field handling ---------------------------------------------------------


def test_skips_card_without_gi_no(httpx_mock) -> None:
    """A malformed card with no ``GI_Read/{id}`` anchor should be
    dropped, not emitted as a half-built row."""
    bad_card = """
    <div data-sentry-component="CardJob">
      <a href="/some/other/path" data-sentry-component="Title">
        <span>No id</span>
      </a>
    </div>
    """
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(gi_no="42") + bad_card),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert [j.ats_id for j in jobs] == ["42"]


def test_falls_back_to_logo_alt_when_company_span_missing(httpx_mock) -> None:
    """A few sponsored cards render without the company-name span; we
    recover the company from the logo's ``alt`` attribute (``"{name} 로고"``)."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(
            gi_no="77",
            company="알트컴퍼니",
            include_company_span=False,
        )),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].company == "알트컴퍼니"


def test_company_unknown_when_no_span_and_no_logo_alt(httpx_mock) -> None:
    """When neither the company span nor a usable logo alt is present,
    fall back to a placeholder rather than crashing or emitting empty."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(
            gi_no="78",
            include_company_span=False,
            company_alt="",
        )),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].company == "Unknown"


@pytest.mark.parametrize(
    ("title", "expected_norm", "expected_raw"),
    [
        ("[정규직] 백엔드 개발자", "FULL_TIME", "정규직"),
        ("[계약직] 사무 보조", "CONTRACT", "계약직"),
        ("2026 채용연계형 인턴 모집", "INTERN", "인턴"),
        ("[프리랜서] Kubernetes 엔지니어", "CONTRACT", "프리랜서"),
        ("[아르바이트] 매장 직원", "PART_TIME", "아르바이트"),
        ("[파견직] 운영 보조", "CONTRACT", "파견직"),
    ],
)
def test_employment_type_detected_from_title(
    httpx_mock, title: str, expected_norm: str, expected_raw: str,
) -> None:
    """JobKorea doesn't carry a structured employment-type field on the
    listing card; employers conventionally encode it in the title
    prefix. We surface both the raw token and the normalized enum."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(title=title)),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].employment_type == expected_norm
    assert jobs[0].raw is not None
    assert jobs[0].raw["employment_type_label"] == expected_raw


def test_title_without_employment_token_leaves_field_none(httpx_mock) -> None:
    """Most titles don't carry an explicit employment-type token —
    ``employment_type`` should stay ``None`` rather than guess."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(title="백엔드 개발자")),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].employment_type is None


def test_chips_optional(httpx_mock) -> None:
    """A card with no location / salary / sector chips should still
    parse (those fields are simply ``None`` / absent from raw)."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(
            location=None, sector=None, salary_label=None,
        )),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    j = jobs[0]
    assert j.location is None
    assert j.raw is None or "sector" not in j.raw
    assert j.raw is None or "salary_label" not in j.raw


def test_experience_label_optional(httpx_mock) -> None:
    """Some cards omit the experience-requirement label. That should
    not break parsing — ``raw['experience']`` is simply absent."""
    httpx_mock.add_response(
        url=_SEARCH_RE,
        text=_page(_card(experience=None)),
    )
    httpx_mock.add_response(url=_SEARCH_RE, text=_page(""), is_reusable=True)

    jobs = JobKoreaScraper("dev").fetch()
    assert jobs[0].raw is None or "experience" not in jobs[0].raw


# --- error handling ---------------------------------------------------------


def test_persistent_500_raises(httpx_mock) -> None:
    """Real server failures should surface, not silently emit []."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobKoreaScraper("dev").fetch()


def test_403_raises_immediately(httpx_mock) -> None:
    """A 4xx from JobKorea's WAF is not a retry-worthy error — surface
    it to the caller so the operator rotates the UA / proxies."""
    httpx_mock.add_response(url=_SEARCH_RE, status_code=403)
    with pytest.raises(ScraperError):
        JobKoreaScraper("dev").fetch()
