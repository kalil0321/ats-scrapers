"""Tests for the generic Eightfold scraper and its tenant subclasses
(Microsoft, Nvidia).

These tests pin four contracts:

1. Construction defaults — what `EightfoldScraper("foo")` resolves to with no
   extra args.
2. Subclass identity — Microsoft and Nvidia keep their stable `ATSType` even
   though the implementation is shared.
3. Parser robustness — Eightfold's response shape varies between tenants
   (string vs dict locations, ms vs sec timestamps, missing fields).
4. Concurrency + WAF behavior — pagination fans out using `data.count`,
   and 403s trigger an automatic httpcloak fallback in the default
   ``client_kind="auto"`` mode.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import (
    EightfoldScraper,
    ScraperRegistry,
    get_scraper,
)
from jobhive.scrapers.eightfold import _extract_remote, _format_location, _parse_ts

# Detail enrichment fires per-job ``position_details`` GET calls after
# the search pass; tests that don't care about description ignore them.
pytestmark = pytest.mark.httpx_mock(
    assert_all_requests_were_expected=False,
)


# --- module-level fixtures --------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries default to 3 with 1.5s base delay → up to 9s per failing test.
    Tests don't need that. Knock retries down to 1 and skip sleeps."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 1)
    monkeypatch.setattr(ef, "RETRY_BASE_DELAY", 0.0)


# --- helpers ----------------------------------------------------------------

URL = "https://dolby.eightfold.ai/api/pcsx/search"


def _position(
    *,
    job_id: str = "100",
    title: str = "Engineer",
    location: str | None = "Remote",
    posted: int | str | None = None,
    position_url: str | None = "/careers/job/100",
) -> dict[str, Any]:
    p: dict[str, Any] = {"displayJobId": job_id, "name": title}
    if location is not None:
        p["locations"] = [location]
    if posted is not None:
        p["postedTs"] = posted
    if position_url is not None:
        p["positionUrl"] = position_url
    return p


def _page(positions: list[dict[str, Any]], *, count: int | None = None) -> dict[str, Any]:
    """Wrap positions in the canonical Eightfold response envelope.

    `count` is the **total job count** that drives fan-out. If omitted, we
    set it to len(positions) so the scraper believes the listing fits in
    one page and stops.
    """
    return {"data": {"positions": positions, "count": count if count is not None else len(positions)}}


def _mock_url(start: int, *, base: str = URL, domain: str = "dolby.com") -> str:
    return (
        f"{base}?domain={domain}&query=&location=&start={start}&sort_by=timestamp"
    )


# --- Construction & defaults -------------------------------------------------


def test_default_base_url_is_eightfold_subdomain() -> None:
    s = EightfoldScraper("dolby")
    assert s.base_url == "https://dolby.eightfold.ai"


def test_default_domain_is_dotcom() -> None:
    s = EightfoldScraper("dolby")
    assert s.domain == "dolby.com"


def test_default_company_name_is_titlecased_slug() -> None:
    s = EightfoldScraper("dolby")
    assert s.company_name == "Dolby"


def test_default_company_name_handles_dashed_slug() -> None:
    s = EightfoldScraper("palo-alto-networks")
    assert s.company_name == "Palo Alto Networks"


def test_default_job_url_host_falls_back_to_base_url() -> None:
    s = EightfoldScraper("dolby")
    assert s.job_url_host == s.base_url


def test_overrides_take_precedence() -> None:
    s = EightfoldScraper(
        "x",
        base_url="https://api.example.com",
        domain="example.io",
        company_name="Example Co",
        job_url_host="https://jobs.example.com",
    )
    assert s.base_url == "https://api.example.com"
    assert s.domain == "example.io"
    assert s.company_name == "Example Co"
    assert s.job_url_host == "https://jobs.example.com"


def test_trailing_slash_stripped_from_urls() -> None:
    s = EightfoldScraper(
        "x",
        base_url="https://api.example.com/",
        job_url_host="https://jobs.example.com/",
    )
    assert s.base_url == "https://api.example.com"
    assert s.job_url_host == "https://jobs.example.com"


def test_company_slug_and_timeout_propagated_to_base() -> None:
    s = EightfoldScraper("dolby", timeout=7.5)
    assert s.company_slug == "dolby"
    assert s.timeout == 7.5


def test_default_client_kind_is_auto() -> None:
    """Default behavior must probe httpx first and fall back to httpcloak
    only on 403. Changing this default would alter the cost profile."""
    assert EightfoldScraper("x").client_kind == "auto"


def test_client_kind_is_settable() -> None:
    assert EightfoldScraper("x", client_kind="httpx").client_kind == "httpx"
    assert EightfoldScraper("x", client_kind="httpcloak").client_kind == "httpcloak"


# --- Custom-domain tenants (e.g. Microsoft) ---------------------------------


def test_microsoft_via_eightfold_with_custom_domain() -> None:
    """Microsoft fronts Eightfold on a custom domain. Library users instantiate
    `EightfoldScraper` with the four overrides — there is no dedicated
    MicrosoftScraper class anymore (kept the dataset on `ats_type=eightfold`)."""
    s = EightfoldScraper(
        "microsoft",
        base_url="https://apply.careers.microsoft.com",
        domain="microsoft.com",
        company_name="Microsoft",
        job_url_host="https://jobs.careers.microsoft.com",
    )
    assert s.base_url == "https://apply.careers.microsoft.com"
    assert s.domain == "microsoft.com"
    assert s.company_name == "Microsoft"
    assert s.job_url_host == "https://jobs.careers.microsoft.com"
    assert s.ats is ATSType.EIGHTFOLD


# --- Registry ----------------------------------------------------------------


def test_registry_has_eightfold() -> None:
    assert ScraperRegistry.get(ATSType.EIGHTFOLD) is EightfoldScraper


def test_get_scraper_by_string_eightfold() -> None:
    s = get_scraper("eightfold", "dolby")
    assert isinstance(s, EightfoldScraper)
    assert s.company_slug == "dolby"


# --- fetch() via httpx: happy path & fan-out --------------------------------


def test_fetch_single_page_when_count_le_page_size(httpx_mock) -> None:
    """count <= 10 → no fan-out, only the start=0 request fires."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(job_id="A"), _position(job_id="B")], count=2),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert [j.ats_id for j in jobs] == ["A", "B"]
    assert jobs[0].title == "Engineer"
    assert jobs[0].company == "Dolby"
    assert jobs[0].ats_type is ATSType.EIGHTFOLD


def test_fetch_fans_out_using_count(httpx_mock) -> None:
    """count=25 → first page (start=0) returns 10, plus offsets 10 and 20."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(10)],
            count=25,
        ),
    )
    httpx_mock.add_response(
        url=_mock_url(10),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(10, 20)],
            count=25,
        ),
    )
    httpx_mock.add_response(
        url=_mock_url(20),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(20, 25)],
            count=25,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert len(jobs) == 25
    # Order isn't guaranteed (concurrent fan-out), but the set must match.
    assert {j.ats_id for j in jobs} == {f"P{i}" for i in range(25)}


def test_fetch_returns_empty_when_first_page_empty(httpx_mock) -> None:
    httpx_mock.add_response(url=_mock_url(0), json=_page([], count=0))
    assert EightfoldScraper("dolby").fetch() == []


def test_fetch_dedupes_jobs_with_same_ats_id(httpx_mock) -> None:
    """If concurrent pages return the same `displayJobId` (the listing can
    shift between requests), the final list must contain each id once."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(10)],
            count=15,
        ),
    )
    # Second page repeats the last 5 of page one
    httpx_mock.add_response(
        url=_mock_url(10),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(5, 15)],
            count=15,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert len({j.ats_id for j in jobs}) == len(jobs)
    assert {j.ats_id for j in jobs} == {f"P{i}" for i in range(15)}


def test_fetch_handles_missing_data_envelope(httpx_mock) -> None:
    httpx_mock.add_response(url=_mock_url(0), json={})
    assert EightfoldScraper("dolby").fetch() == []


def test_fetch_handles_zero_count_on_first_page(httpx_mock) -> None:
    """count=0 with empty positions — early-exit short-circuit."""
    httpx_mock.add_response(url=_mock_url(0), json=_page([], count=0))
    assert EightfoldScraper("dolby").fetch() == []


def test_fetch_handles_count_exactly_at_page_size_boundary(httpx_mock) -> None:
    """count == PAGE_SIZE (10) means exactly one page. No fan-out should fire
    — if it did, the test would error on a missing mock for start=10."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(job_id=f"P{i}") for i in range(10)], count=10),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert len(jobs) == 10


# --- fetch(): custom-domain tenants emit the right URL host -----------------


def test_microsoft_via_custom_domain_keeps_eightfold_ats_type(httpx_mock) -> None:
    """Microsoft jobs are tagged `eightfold` (the underlying ATS) and use
    the public job-rendering host, not the API host."""
    api = "https://apply.careers.microsoft.com/api/pcsx/search"
    httpx_mock.add_response(
        url=_mock_url(0, base=api, domain="microsoft.com"),
        json=_page([_position(job_id="MS-1")], count=1),
    )
    jobs = EightfoldScraper(
        "microsoft",
        base_url="https://apply.careers.microsoft.com",
        domain="microsoft.com",
        company_name="Microsoft",
        job_url_host="https://jobs.careers.microsoft.com",
    ).fetch()
    assert jobs[0].ats_type is ATSType.EIGHTFOLD
    assert jobs[0].company == "Microsoft"
    assert str(jobs[0].url).startswith("https://jobs.careers.microsoft.com")


# --- _parse_job: URL resolution ----------------------------------------------


def test_parse_job_relative_position_url_uses_job_url_host(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(job_id="X", position_url="/careers/job/X")],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert str(jobs[0].url) == "https://dolby.eightfold.ai/careers/job/X"


def test_parse_job_absolute_position_url_used_as_is(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [
                _position(
                    job_id="X",
                    position_url="https://elsewhere.example.com/job/X",
                )
            ],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert str(jobs[0].url) == "https://elsewhere.example.com/job/X"


def test_parse_job_missing_position_url_falls_back_to_synthetic(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(job_id="X", position_url=None)],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert str(jobs[0].url) == "https://dolby.eightfold.ai/careers/job/X"


def test_custom_domain_uses_separate_jobs_host(httpx_mock) -> None:
    """When `job_url_host` differs from `base_url` (Microsoft's setup),
    relative `positionUrl`s must be prepended with the job host, not the
    API host."""
    api = "https://apply.careers.microsoft.com/api/pcsx/search"
    httpx_mock.add_response(
        url=_mock_url(0, base=api, domain="microsoft.com"),
        json=_page(
            [{"displayJobId": "X", "name": "X", "positionUrl": "/careers/job/X"}],
            count=1,
        ),
    )
    jobs = EightfoldScraper(
        "microsoft",
        base_url="https://apply.careers.microsoft.com",
        domain="microsoft.com",
        company_name="Microsoft",
        job_url_host="https://jobs.careers.microsoft.com",
    ).fetch()
    assert str(jobs[0].url) == "https://jobs.careers.microsoft.com/careers/job/X"


# --- _parse_job: ats_id fallback chain ---------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_id"),
    [
        ({"displayJobId": "DJ", "id": "I", "atsJobId": "A"}, "DJ"),
        ({"id": "I", "atsJobId": "A"}, "I"),
        ({"atsJobId": "A"}, "A"),
        ({}, ""),
    ],
)
def test_parse_job_ats_id_fallback_chain(
    httpx_mock, payload: dict[str, Any], expected_id: str
) -> None:
    payload = {**payload, "name": "X", "positionUrl": "/job/x"}
    httpx_mock.add_response(url=_mock_url(0), json=_page([payload], count=1))
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].ats_id == expected_id


def test_parse_job_title_fallback_to_untitled(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [{"displayJobId": "X", "positionUrl": "/job/x"}],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].title == "Untitled"


def test_parse_job_prefers_name_over_title_field(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [
                {
                    "displayJobId": "X",
                    "name": "From name",
                    "title": "From title",
                    "positionUrl": "/job/x",
                }
            ],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].title == "From name"


# --- _format_location: helper unit tests -------------------------------------


def test_format_location_prefers_standardized_over_locations() -> None:
    assert _format_location(
        {"standardizedLocations": ["Standardized City"], "locations": ["Raw City"]}
    ) == "Standardized City"


def test_format_location_string_list_returns_first_stripped() -> None:
    assert _format_location({"locations": ["  Berlin  "]}) == "Berlin"


def test_format_location_dict_list_uses_city() -> None:
    assert _format_location({"locations": [{"city": "Paris", "country": "FR"}]}) == "Paris"


def test_format_location_dict_list_falls_back_to_country() -> None:
    assert _format_location({"locations": [{"country": "FR"}]}) == "FR"


def test_format_location_dict_list_falls_back_to_name() -> None:
    assert _format_location({"locations": [{"name": "Some Region"}]}) == "Some Region"


def test_format_location_primary_location_dict() -> None:
    assert _format_location({"primaryLocation": {"city": "Tokyo"}}) == "Tokyo"


def test_format_location_primary_location_string() -> None:
    assert _format_location({"primaryLocation": "Singapore"}) == "Singapore"


def test_format_location_snake_case_key_alias() -> None:
    assert _format_location({"primary_location": {"city": "Madrid"}}) == "Madrid"


def test_format_location_returns_none_when_all_missing() -> None:
    assert _format_location({}) is None


def test_format_location_skips_empty_string_in_list() -> None:
    assert _format_location(
        {"standardizedLocations": ["   "], "locations": ["Berlin"]}
    ) == "Berlin"


def test_format_location_empty_list_falls_through() -> None:
    assert _format_location(
        {"standardizedLocations": [], "locations": ["Berlin"]}
    ) == "Berlin"


# --- _parse_ts: timestamp helper ---------------------------------------------


def test_parse_ts_iso_string_with_z_suffix() -> None:
    result = _parse_ts("2026-04-01T12:00:00Z")
    assert result is not None
    assert result.replace(tzinfo=None) == datetime(2026, 4, 1, 12, 0, 0)
    assert result.tzinfo is not None


def test_parse_ts_iso_string_with_offset() -> None:
    result = _parse_ts("2026-04-01T12:00:00+02:00")
    assert result is not None
    assert result.astimezone(UTC).replace(tzinfo=None) == datetime(
        2026, 4, 1, 10, 0, 0
    )


def test_parse_ts_unix_seconds() -> None:
    result = _parse_ts(1767225600)
    assert result is not None
    assert result.year == 2026


def test_parse_ts_unix_milliseconds() -> None:
    """Values > 1e10 are treated as milliseconds (Eightfold's default)."""
    result = _parse_ts(1767225600000)
    assert result is not None
    assert result.year == 2026


def test_parse_ts_none_returns_none() -> None:
    assert _parse_ts(None) is None


def test_parse_ts_invalid_string_returns_none() -> None:
    assert _parse_ts("not a date") is None


def test_parse_ts_creation_ts_used_when_posted_ts_missing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [
                {
                    "displayJobId": "X",
                    "name": "X",
                    "positionUrl": "/job/x",
                    "creationTs": 1767225600000,
                }
            ],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].posted_at is not None
    assert jobs[0].posted_at.year == 2026


# --- HTTP error handling -----------------------------------------------------


def test_fetch_raises_on_404(httpx_mock) -> None:
    """404 is non-retryable (the tenant doesn't exist) — fail fast."""
    httpx_mock.add_response(url=_mock_url(0), status_code=404)
    with pytest.raises(ScraperError, match="404"):
        EightfoldScraper("dolby").fetch()


def test_fetch_raises_on_5xx(httpx_mock) -> None:
    httpx_mock.add_response(url=_mock_url(0), status_code=503, is_reusable=True)
    with pytest.raises(ScraperError, match="503"):
        EightfoldScraper("dolby").fetch()


def test_fetch_raises_on_network_failure(httpx_mock) -> None:
    httpx_mock.add_exception(
        httpx.ConnectError("DNS lookup failed"),
        url=_mock_url(0),
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="DNS lookup failed"):
        EightfoldScraper("dolby").fetch()


def test_fetch_error_message_includes_company_name(httpx_mock) -> None:
    """When debugging across many tenants, the error must say WHICH tenant
    failed."""
    httpx_mock.add_response(url=_mock_url(0), status_code=500, is_reusable=True)
    with pytest.raises(ScraperError, match="Dolby"):
        EightfoldScraper("dolby").fetch()


def test_fetch_error_on_paginated_page_includes_offset(httpx_mock) -> None:
    """A failure mid-pagination should mention the start offset for triage."""
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [_position(job_id=f"P{i}") for i in range(10)],
            count=20,
        ),
    )
    httpx_mock.add_response(url=_mock_url(10), status_code=500, is_reusable=True)
    with pytest.raises(ScraperError, match="start=10"):
        EightfoldScraper("dolby").fetch()


# --- Retry behavior (ported from legacy Microsoft scraper) ------------------


def test_retries_on_500_then_succeeds(monkeypatch, httpx_mock) -> None:
    """Transient 500 → retry → succeed. The legacy Microsoft scraper hits
    this path on ~1% of requests; without retries the whole scrape fails."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 3)

    httpx_mock.add_response(url=_mock_url(0), status_code=500)
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(job_id="OK")], count=1),
    )

    jobs = EightfoldScraper("dolby").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


def test_retries_on_429_then_succeeds(monkeypatch, httpx_mock) -> None:
    """Rate limits should back off and retry, not crash the run."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 3)

    httpx_mock.add_response(url=_mock_url(0), status_code=429)
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(job_id="OK")], count=1),
    )

    jobs = EightfoldScraper("dolby").fetch()
    assert [j.ats_id for j in jobs] == ["OK"]


def test_429_with_retry_after_header_is_honored(monkeypatch, httpx_mock) -> None:
    """When the server tells us how long to wait, we should honour it
    rather than apply our own backoff."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 3)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=_mock_url(0),
        status_code=429,
        headers={"Retry-After": "7"},
    )
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page([_position(job_id="OK")], count=1),
    )

    EightfoldScraper("dolby").fetch()
    assert 7.0 in sleeps  # we honoured the server hint


def test_retries_exhausted_raises(monkeypatch, httpx_mock) -> None:
    """All 3 attempts return 500 → final ScraperError mentions retries."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 3)

    httpx_mock.add_response(url=_mock_url(0), status_code=500, is_reusable=True)

    with pytest.raises(ScraperError, match="retries"):
        EightfoldScraper("dolby").fetch()


def test_404_does_not_trigger_retries(monkeypatch, httpx_mock) -> None:
    """404 means "this tenant doesn't exist" — retrying is wasted time."""
    import jobhive.scrapers.eightfold as ef
    monkeypatch.setattr(ef, "MAX_RETRIES", 3)

    # Only ONE 404 mock — if we retry, the second request has no mock and the
    # test fails with a different (much louder) error.
    httpx_mock.add_response(url=_mock_url(0), status_code=404)

    with pytest.raises(ScraperError, match="404"):
        EightfoldScraper("dolby").fetch()


# --- Extra fields extraction (department, is_remote) -----------------------


def test_parses_department_field(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [
                {
                    "displayJobId": "X",
                    "name": "X",
                    "positionUrl": "/job/x",
                    "department": "Engineering",
                }
            ],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].department == "Engineering"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workLocationOption", "Remote", True),
        ("workLocationOption", "Up to 100% work from home", True),
        ("workLocationOption", "Hybrid", None),  # ambiguous on purpose
        ("workLocationOption", "Onsite", False),
        ("workLocationOption", "On-site", False),
        ("locationFlexibility", "Fully remote", True),
        ("locationFlexibility", "In office", False),
        ("workLocationOption", "", None),
        ("workLocationOption", "Flexible", None),  # unknown value
    ],
)
def test_extract_remote_normalizes_eightfold_strings(
    field: str, value: str, expected: bool | None
) -> None:
    assert _extract_remote({field: value}) is expected


def test_extract_remote_returns_none_on_missing() -> None:
    assert _extract_remote({}) is None
    assert _extract_remote({"workLocationOption": None}) is None
    assert _extract_remote({"workLocationOption": 42}) is None  # type: ignore[dict-item]


def test_is_remote_propagates_to_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_mock_url(0),
        json=_page(
            [
                {
                    "displayJobId": "X",
                    "name": "X",
                    "positionUrl": "/job/x",
                    "workLocationOption": "Remote",
                }
            ],
            count=1,
        ),
    )
    jobs = EightfoldScraper("dolby").fetch()
    assert jobs[0].is_remote is True


# --- WAF (403) behavior: pinned vs auto -------------------------------------


def test_client_kind_httpx_raises_explicit_error_on_403(httpx_mock) -> None:
    """If the user pins to httpx, they want the 403 surfaced — not silently
    swallowed by a fallback they didn't ask for."""
    httpx_mock.add_response(url=_mock_url(0), status_code=403)
    with pytest.raises(ScraperError, match="WAF"):
        EightfoldScraper("dolby", client_kind="httpx").fetch()


def test_auto_falls_back_to_httpcloak_on_403(monkeypatch, httpx_mock) -> None:
    """auto mode + httpx 403 → must invoke the httpcloak path. We stub
    httpcloak with a fake module that returns a normal response; if the
    fallback works, fetch() succeeds with jobs from the stub."""
    httpx_mock.add_response(url=_mock_url(0), status_code=403)

    fake_httpcloak = SimpleNamespace(
        get=lambda url, params, headers, timeout: SimpleNamespace(
            status_code=200,
            json=lambda: _page([_position(job_id="HC-1")], count=1),
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)

    jobs = EightfoldScraper("dolby").fetch()  # client_kind="auto" by default
    assert len(jobs) == 1
    assert jobs[0].ats_id == "HC-1"


def test_client_kind_httpcloak_skips_httpx_probe(monkeypatch) -> None:
    """When pinned to httpcloak, the scraper must NOT call httpx — even once.
    We replace httpx.AsyncClient with a sentinel that would fail the test
    if instantiated, and stub httpcloak with a normal response."""
    import jobhive.scrapers.eightfold as ef_mod

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("httpx must not be called when client_kind=httpcloak")

    monkeypatch.setattr(ef_mod.httpx, "AsyncClient", boom)
    fake_httpcloak = SimpleNamespace(
        get=lambda url, params, headers, timeout: SimpleNamespace(
            status_code=200,
            json=lambda: _page([_position(job_id="HC-2")], count=1),
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)

    jobs = EightfoldScraper("dolby", client_kind="httpcloak").fetch()
    assert [j.ats_id for j in jobs] == ["HC-2"]


def test_httpcloak_fan_out_paginates_sequentially(monkeypatch) -> None:
    """httpcloak path is sync — verify it walks through all offsets and
    aggregates. count=22, so we expect requests at start=0, 10, 20."""
    calls: list[int] = []

    def fake_get(url: str, params: dict[str, Any], headers: dict[str, Any], timeout: float) -> Any:
        start = int(params["start"])
        calls.append(start)
        positions = [
            _position(job_id=f"P{start + i}") for i in range(min(10, 22 - start))
        ]
        return SimpleNamespace(
            status_code=200,
            json=lambda: _page(positions, count=22),
        )

    monkeypatch.setitem(sys.modules, "httpcloak", SimpleNamespace(get=fake_get))

    jobs = EightfoldScraper("dolby", client_kind="httpcloak").fetch()
    assert sorted(calls) == [0, 10, 20]
    assert len(jobs) == 22


def test_httpcloak_raises_when_module_missing(monkeypatch) -> None:
    """If `pip install httpcloak` was skipped, the error must explain how
    to fix it — not just `ModuleNotFoundError`."""
    # Simulate import-time failure: poison sys.modules so import fails
    monkeypatch.setitem(sys.modules, "httpcloak", None)
    with pytest.raises(ScraperError, match="httpcloak"):
        EightfoldScraper("dolby", client_kind="httpcloak").fetch()


def test_httpcloak_non_200_raises(monkeypatch) -> None:
    """An httpcloak response that's not 200 must surface as a ScraperError
    that names the tenant + offset."""
    fake_httpcloak = SimpleNamespace(
        get=lambda url, params, headers, timeout: SimpleNamespace(
            status_code=503,
            json=lambda: {},
        )
    )
    monkeypatch.setitem(sys.modules, "httpcloak", fake_httpcloak)
    with pytest.raises(ScraperError, match=r"Dolby.*503"):
        EightfoldScraper("dolby", client_kind="httpcloak").fetch()
