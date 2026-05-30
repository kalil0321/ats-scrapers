"""Tests for the WorkNet (Korea, work24.go.kr) scraper.

The open API is XML-only and key-gated; these tests pin the parsing
contract, the env-var gate, and the error-code handling that
distinguishes a clean empty result from a contract break (invalid key,
exhausted quota) that must crash rather than silently return ``[]``.
"""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import ScraperRegistry, WorkNetKoreaScraper

_API_RE = re.compile(r"^https://openapi\.work\.go\.kr/opi/opi/opia/wantedApi\.do")


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-empty key so tests don't all have to set it.

    Tests that exercise the missing-key path explicitly ``delenv`` first.
    """
    monkeypatch.setenv("WORKNET_API_KEY", "TEST_KEY")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.worknet_kr as wn
    monkeypatch.setattr(wn, "MAX_RETRIES", 1)
    monkeypatch.setattr(wn, "RETRY_BASE_DELAY", 0.0)


def _wanted_xml(rows: list[dict[str, str]]) -> str:
    """Build a ``<wantedRoot>`` body the way the open API emits it."""
    elements = []
    for row in rows:
        children = "".join(
            f"<{k}>{v}</{k}>" for k, v in row.items()
        )
        elements.append(f"<wanted>{children}</wanted>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<wantedRoot>{''.join(elements)}</wantedRoot>"
    )


_DEFAULT_ROW: dict[str, str] = {
    "wantedAuthNo": "K162825302008",
    "empWantedTitle": "데이터 엔지니어",
    "coNm": "한국전자통신연구원",
    "workRegion": "서울특별시 강남구",
    "regDt": "20260501",
    "empWantedTypeNm": "정규직",
    "sal": "연 4,500만원 이상",
    "salTpNm": "연봉",
    "jobsCd": "133100",
    "salTpCd": "Y",
    "indCd": "62010",
    "career": "신입",
}


def _row(**overrides: str) -> dict[str, str]:
    """Build one ``<wanted>`` row by overlaying overrides on the
    default Korean-public-sector posting fixture.

    Pass field names exactly as the WorkNet API emits them (camelCase
    Korean) — they propagate verbatim into the XML body.
    """
    base = dict(_DEFAULT_ROW)
    base.update(overrides)
    return base


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_worknet() -> None:
    assert ScraperRegistry.get(ATSType.WORKNETKR) is WorkNetKoreaScraper


def test_ats_type_enum_value() -> None:
    """The enum value travels into the public dataset; pin it."""
    assert ATSType.WORKNETKR.value == "worknet_kr"


# --- env-var gate -----------------------------------------------------------


def test_missing_api_key_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKNET_API_KEY", raising=False)
    with pytest.raises(ScraperError) as excinfo:
        WorkNetKoreaScraper("any").fetch()
    assert "WORKNET_API_KEY" in str(excinfo.value)


def test_empty_api_key_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only key counts as missing."""
    monkeypatch.setenv("WORKNET_API_KEY", "   ")
    with pytest.raises(ScraperError) as excinfo:
        WorkNetKoreaScraper("any").fetch()
    assert "WORKNET_API_KEY" in str(excinfo.value)


# --- parsing happy path ----------------------------------------------------


def test_parses_full_xml_row(httpx_mock) -> None:
    """Single-page scrape; verify every populated Job field maps to the
    right WorkNet XML element."""
    httpx_mock.add_response(
        url=_API_RE,
        content=_wanted_xml([_row()]).encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        is_reusable=True,
    )
    jobs = WorkNetKoreaScraper("any").fetch()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.ats_id == "K162825302008"
    assert job.ats_type is ATSType.WORKNETKR
    assert job.title == "데이터 엔지니어"
    assert job.company == "한국전자통신연구원"
    assert job.location == "서울특별시 강남구"
    assert job.country_iso == "KR"
    assert job.language == "ko"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "정규직"
    assert job.salary_currency == "KRW"
    assert job.salary_summary == "연 4,500만원 이상"
    assert job.posted_at is not None
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 5
    assert job.posted_at.day == 1
    # Detail URL falls back to work24.go.kr when the API doesn't
    # expose a direct link.
    assert "K162825302008" in str(job.url)
    assert "work24.go.kr" in str(job.url)
    assert job.raw == {
        "job_type_code": "133100",
        "wage_code": "Y",
        "industry_code": "62010",
        "career_level": "신입",
    }


def test_detail_url_prefers_api_provided_link(httpx_mock) -> None:
    """When the API returns a fully qualified detail URL, use it
    verbatim instead of the work24 fallback."""
    httpx_mock.add_response(
        url=_API_RE,
        content=_wanted_xml([
            _row(empWantedHomepgDetail="https://example.com/jobs/abc"),
        ]).encode("utf-8"),
    )
    jobs = WorkNetKoreaScraper("any").fetch()
    assert str(jobs[0].url).rstrip("/") == "https://example.com/jobs/abc"


def test_employment_type_label_mapping(httpx_mock) -> None:
    """Korean employment-type labels map to the canonical enum;
    commitment keeps the original Korean text."""
    httpx_mock.add_response(
        url=_API_RE,
        content=_wanted_xml([
            _row(wantedAuthNo="A1", empWantedTypeNm="정규직"),
            _row(wantedAuthNo="A2", empWantedTypeNm="계약직"),
            _row(wantedAuthNo="A3", empWantedTypeNm="인턴"),
            _row(wantedAuthNo="A4", empWantedTypeNm="아르바이트"),
            _row(wantedAuthNo="A5", empWantedTypeNm="파견직"),
        ]).encode("utf-8"),
    )
    jobs = WorkNetKoreaScraper("any").fetch()
    by_id = {j.ats_id: j for j in jobs}
    assert by_id["A1"].employment_type == "FULL_TIME"
    assert by_id["A2"].employment_type == "CONTRACT"
    assert by_id["A3"].employment_type == "INTERN"
    assert by_id["A4"].employment_type == "PART_TIME"
    assert by_id["A5"].employment_type == "TEMPORARY"
    assert by_id["A1"].commitment == "정규직"


def test_missing_required_fields_drops_row(httpx_mock) -> None:
    """A row without ``wantedAuthNo`` or ``empWantedTitle`` is unusable
    and must be skipped silently."""
    httpx_mock.add_response(
        url=_API_RE,
        content=_wanted_xml([
            {"empWantedTitle": "No ID"},  # missing wantedAuthNo
            {"wantedAuthNo": "K9"},  # missing title
            _row(wantedAuthNo="K10", empWantedTitle="Valid"),
        ]).encode("utf-8"),
    )
    jobs = WorkNetKoreaScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"K10"}


def test_no_salary_no_currency(httpx_mock) -> None:
    """When the row has no salary text, we don't attach a phantom KRW
    currency — that would lie about the row having compensation data."""
    httpx_mock.add_response(
        url=_API_RE,
        content=_wanted_xml([
            {
                "wantedAuthNo": "K1",
                "empWantedTitle": "Engineer",
                "coNm": "Acme",
            },
        ]).encode("utf-8"),
    )
    jobs = WorkNetKoreaScraper("any").fetch()
    assert jobs[0].salary_currency is None
    assert jobs[0].salary_summary is None


# --- pagination -------------------------------------------------------------


def test_pagination_terminates_on_short_page(httpx_mock) -> None:
    """The API has no explicit ``last`` flag — pagination stops when a
    page returns fewer rows than ``display``."""
    page1 = _wanted_xml([_row(wantedAuthNo=f"P1-{i}") for i in range(100)])
    page2 = _wanted_xml([_row(wantedAuthNo=f"P2-{i}") for i in range(3)])
    httpx_mock.add_response(url=_API_RE, content=page1.encode("utf-8"))
    httpx_mock.add_response(url=_API_RE, content=page2.encode("utf-8"))
    jobs = WorkNetKoreaScraper("any").fetch()
    assert len(jobs) == 103


def test_pagination_terminates_on_empty_page(httpx_mock) -> None:
    """An empty ``<wantedRoot></wantedRoot>`` response (zero <wanted>
    children) is the natural end-of-stream — return what we've got."""
    page1 = _wanted_xml([_row(wantedAuthNo="K-1")])
    page2 = '<?xml version="1.0" encoding="UTF-8"?><wantedRoot></wantedRoot>'
    httpx_mock.add_response(url=_API_RE, content=page1.encode("utf-8"))
    httpx_mock.add_response(url=_API_RE, content=page2.encode("utf-8"))

    scraper = WorkNetKoreaScraper("any", page_size=1)
    jobs = scraper.fetch()
    assert {j.ats_id for j in jobs} == {"K-1"}


def test_dedup_across_pages(httpx_mock) -> None:
    """If the API returns the same row twice across pages, ats_id-based
    dedup drops the repeat."""
    repeated = _row(wantedAuthNo="K-DUP")
    page1 = _wanted_xml([repeated, _row(wantedAuthNo="K-A")])
    # Page 2 sends only the duplicate (short page → terminates).
    page2 = _wanted_xml([repeated])
    httpx_mock.add_response(url=_API_RE, content=page1.encode("utf-8"))
    httpx_mock.add_response(url=_API_RE, content=page2.encode("utf-8"))

    scraper = WorkNetKoreaScraper("any", page_size=2)
    jobs = scraper.fetch()
    assert sorted(j.ats_id for j in jobs) == ["K-A", "K-DUP"]


# --- contract-break failures: must crash, not soft-fail --------------------


def test_invalid_key_application_error_crashes(httpx_mock) -> None:
    """API error code 002 (invalid key) is a contract break — the
    scraper must raise, not silently return ``[]``. A silent zero-result
    response would publish a wholesale undercount as a successful run."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<wantedRoot>"
        "<message>유효하지 않은 인증키 입니다.</message>"
        "<messageCd>002</messageCd>"
        "</wantedRoot>"
    )
    httpx_mock.add_response(url=_API_RE, content=body.encode("utf-8"))
    with pytest.raises(ScraperError) as excinfo:
        WorkNetKoreaScraper("any").fetch()
    assert "002" in str(excinfo.value)


def test_quota_exceeded_application_error_crashes(httpx_mock) -> None:
    """Quota exhaustion (msgCd=003) is loud-fail too — operator should
    see it, not have it papered over as a successful empty run."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<wantedRoot>"
        "<message>일일 트래픽 한도를 초과했습니다.</message>"
        "<messageCd>003</messageCd>"
        "</wantedRoot>"
    )
    httpx_mock.add_response(url=_API_RE, content=body.encode("utf-8"))
    with pytest.raises(ScraperError) as excinfo:
        WorkNetKoreaScraper("any").fetch()
    assert "003" in str(excinfo.value)


def test_malformed_xml_crashes(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        content=b"<html>Maintenance</html><<not-xml>>",
    )
    with pytest.raises(ScraperError):
        WorkNetKoreaScraper("any").fetch()


def test_http_500_crashes_after_retries(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        WorkNetKoreaScraper("any").fetch()
