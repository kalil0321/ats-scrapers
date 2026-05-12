"""Tests for the Kariyer.net scraper.

Scope: httpcloak gating (graceful degradation when the optional
TLS-impersonation HTTP client is missing), entry parsing, pagination
dedup, and retry/backoff behaviour. The httpcloak network path is
exercised via a stub session — we never hit the live API in tests.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import KariyerScraper, ScraperRegistry
from jobhive.scrapers import kariyer as k_mod

# --- Stub session --------------------------------------------------------


class _StubResponse:
    """Mimics ``httpcloak.Response`` enough for the scraper's needs."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text or ""

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _StubSession:
    """Stand-in for ``httpcloak.Session``. Records every POST so tests
    can assert on body shape, then replays canned responses in order.

    The scraper uses it via context-manager protocol so we honour that.
    """

    def __init__(self, responses: list[_StubResponse]) -> None:
        self._responses = list(responses)
        self.posts: list[dict[str, Any]] = []

    def __enter__(self) -> _StubSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def post(
        self, url: str, *, json: dict, headers: dict, timeout: float,
    ) -> _StubResponse:
        self.posts.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if not self._responses:
            # Test bug: we shouldn't run out of canned responses.
            raise AssertionError("ran out of stubbed responses")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps add seconds per retry — patch to a no-op so the
    retry-path tests don't pay wall-clock cost."""
    monkeypatch.setattr(k_mod, "_sleep_backoff", lambda attempt: None)


def _install_session(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StubResponse],
) -> _StubSession:
    """Pretend ``httpcloak.Session(preset=...)`` returns our stub."""
    session = _StubSession(responses)

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError  # surface a type we can raise

        @staticmethod
        def Session(preset: str) -> _StubSession:  # noqa: N802 - mimic API
            session._preset = preset  # type: ignore[attr-defined]
            return session

    # Force the scraper's two-stage import (``import httpcloak`` inside
    # the method) to find our stub. ``is_enabled`` does the same import
    # at the gate, so we patch ``_httpcloak_available`` too.
    monkeypatch.setattr(k_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "httpcloak", _StubHttpCloak)
    return session


# --- Fixture payloads ---------------------------------------------------


def _make_item(**overrides: Any) -> dict[str, Any]:
    """One realistic ``data.jobs.items[*]`` row. Override anything per
    test to keep the assertions tight."""
    base = {
        "id": 4449786,
        "title": "Sales Engineer",
        "companyName": "ContiTech Lastik Sanayi ve Ticaret A.Ş.",
        "jobUrl": "/is-ilani/contitech-sales-engineer-4449786",
        "logoUrl": "",
        "locationText": "Bursa",
        "isSponsored": False,
        "workType": "FullTime",
        "workTypeText": "Tam Zamanlı",
        "workModel": "OnSite",
        "postingDate": "2026-05-12",
        "showTime": "2026-05-07T15:41",
        "positionLevel": 3,
        "positionName": "Satış Mühendisi",
        "jobCode": "RF-HK53912",
        "onlyPublishedOnKariyerNet": True,
        "sectors": [{"code": "013000000", "name": "Otomotiv"}],
        "locations": [
            {
                "countryId": "65",
                "countryName": "Türkiye",
                "cityId": "16",
                "cityName": "Bursa",
            }
        ],
        "jobDateText": "5 saat",
    }
    base.update(overrides)
    return base


def _wrap(items: list[dict], total: int | None = None) -> dict:
    return {
        "statusCode": "Success",
        "status": "Success",
        "data": {
            "totalJobCount": total if total is not None else len(items),
            "totalJobCountWithOutSponsored": (
                total if total is not None else len(items)
            ),
            "jobs": {"items": items},
        },
    }


# --- Registry / graceful degradation ------------------------------------


def test_registry_resolves_kariyer() -> None:
    assert ScraperRegistry.get(ATSType.KARIYER) is KariyerScraper


def test_returns_empty_with_warning_when_httpcloak_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``httpcloak`` isn't installed, log a warning and return
    ``[]`` so the publish pipeline keeps moving — same contract as
    cloakbrowser-backed scrapers."""
    monkeypatch.setattr(k_mod, "_httpcloak_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        jobs = KariyerScraper("any").fetch()
    assert jobs == []
    assert any("httpcloak required" in r.getMessage().lower() for r in caplog.records)


# --- Construction / validation ------------------------------------------


def test_rejects_invalid_page_size() -> None:
    with pytest.raises(ScraperError):
        KariyerScraper("any", page_size=0)
    with pytest.raises(ScraperError):
        KariyerScraper("any", page_size=10001)


# --- Parsing ------------------------------------------------------------


def test_parses_minimal_realistic_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            _StubResponse(json_data=_wrap([_make_item()])),
            _StubResponse(json_data=_wrap([])),  # empty page → break
        ],
    )

    jobs = KariyerScraper("any", page_size=100, max_pages=5).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.KARIYER
    assert j.ats_id == "4449786"
    assert j.title == "Sales Engineer"
    assert j.company == "ContiTech Lastik Sanayi ve Ticaret A.Ş."
    assert str(j.url) == (
        "https://www.kariyer.net/is-ilani/contitech-sales-engineer-4449786"
    )
    assert j.location == "Bursa"
    assert j.country_iso == "TR"
    assert j.region == "Asia"
    assert j.language == "tr"
    assert j.employment_type == "FULL_TIME"
    assert j.commitment == "Tam Zamanlı"
    assert j.team == "Satış Mühendisi"
    assert j.department == "Otomotiv"
    # OnSite → is_remote stays None so the title-based heuristic
    # downstream still has room to upgrade it.
    assert j.is_remote is None
    assert j.posted_at == datetime(2026, 5, 12, tzinfo=UTC)
    assert j.fetched_at is not None
    # raw overflow preserves source-specific signals.
    assert j.raw is not None
    assert j.raw["work_model"] == "OnSite"
    assert j.raw["position_level"] == 3
    assert j.raw["is_sponsored"] is False


def test_remote_and_hybrid_workmodel_set_is_remote_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                json_data=_wrap(
                    [
                        _make_item(id=1, workModel="Remote"),
                        _make_item(id=2, workModel="Hybrid"),
                        _make_item(id=3, workModel="OnSite"),
                    ]
                )
            ),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    jobs = sorted(
        KariyerScraper("any", max_pages=5).fetch(),
        key=lambda j: int(j.ats_id),
    )
    assert [j.is_remote for j in jobs] == [True, True, None]


def test_employment_type_map_covers_all_known_worktypes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                json_data=_wrap(
                    [
                        _make_item(id=1, workType="FullTime"),
                        _make_item(id=2, workType="PartTime"),
                        _make_item(id=3, workType="Freelance"),
                        _make_item(id=4, workType="Periodical"),
                        _make_item(id=5, workType="Internship"),
                        _make_item(id=6, workType="MysteryType"),
                    ]
                )
            ),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    jobs = sorted(
        KariyerScraper("any", max_pages=5).fetch(),
        key=lambda j: int(j.ats_id),
    )
    assert [j.employment_type for j in jobs] == [
        "FULL_TIME", "PART_TIME", "CONTRACT", "TEMPORARY", "INTERN", None,
    ]


def test_northern_cyprus_maps_to_cy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small minority of postings are in Northern Cyprus; the API
    surfaces ``Kuzey Kıbrıs T.C`` as ``countryName``. ISO 3166-1
    doesn't recognise TRNC separately so we use ``CY``."""
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                json_data=_wrap(
                    [
                        _make_item(
                            id=99,
                            locations=[
                                {
                                    "countryId": "99",
                                    "countryName": "Kuzey Kıbrıs T.C",
                                    "cityId": "1",
                                    "cityName": "Lefkoşa",
                                }
                            ],
                        )
                    ]
                )
            ),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    [job] = KariyerScraper("any", max_pages=5).fetch()
    assert job.country_iso == "CY"
    # Cyprus is in Europe, not Asia — only TR gets the Asia mapping.
    assert job.region is None


def test_skips_entries_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidential postings sometimes ship a partial row (no title /
    no jobUrl). Skip rather than fake values."""
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                json_data=_wrap(
                    [
                        _make_item(),               # valid
                        _make_item(id=2, title=""), # missing title
                        _make_item(id=3, jobUrl=""),  # missing jobUrl
                        {"id": 4},                  # nearly empty
                    ]
                )
            ),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    jobs = KariyerScraper("any", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["4449786"]


def test_parse_handles_absolute_joburl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if Kariyer ever returns an absolute URL instead of
    a site-relative path, don't double-prefix the host."""
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                json_data=_wrap(
                    [
                        _make_item(
                            id=42,
                            jobUrl="https://www.kariyer.net/is-ilani/x-42",
                        )
                    ]
                )
            ),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    [job] = KariyerScraper("any", max_pages=5).fetch()
    assert str(job.url) == "https://www.kariyer.net/is-ilani/x-42"


# --- Pagination & dedup --------------------------------------------------


def test_paginates_until_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scraper walks pages 1..N until the response yields zero
    items, then stops. ``currentPage`` is sent with each POST."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(json_data=_wrap([_make_item(id=1)])),
            _StubResponse(json_data=_wrap([_make_item(id=2)])),
            _StubResponse(json_data=_wrap([])),  # terminate
        ],
    )
    jobs = KariyerScraper("any", page_size=50, max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]
    # currentPage incremented across calls.
    assert [p["json"]["currentPage"] for p in session.posts] == [1, 2, 3]
    # Body shape includes the anonymous-member sentinel.
    assert all(p["json"]["memberId"] == 0 for p in session.posts)
    # page_size passed verbatim as ``size``.
    assert all(p["json"]["size"] == 50 for p in session.posts)
    # ClientType header sent on every call.
    assert all(p["headers"]["ClientType"] == "1" for p in session.posts)


def test_dedupes_sticky_sponsored_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ~3 sponsored rows repeat on every page. They must surface
    once, not once-per-page."""
    sticky = _make_item(id=999, isSponsored=True)
    _install_session(
        monkeypatch,
        [
            _StubResponse(json_data=_wrap([sticky, _make_item(id=1)])),
            _StubResponse(json_data=_wrap([sticky, _make_item(id=2)])),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    jobs = KariyerScraper("any", max_pages=5).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "999"]


def test_stops_when_whole_page_is_dupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the tail of the catalogue every entry is a sticky repeat.
    The loop must break instead of walking another 1000 empty pages."""
    sticky = _make_item(id=999, isSponsored=True)
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(json_data=_wrap([sticky, _make_item(id=1)])),
            _StubResponse(json_data=_wrap([sticky])),  # no fresh ids
            # Should never request more.
        ],
    )
    jobs = KariyerScraper("any", max_pages=99).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "999"]
    assert len(session.posts) == 2


def test_respects_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving API could keep returning fresh ids forever. The
    safety cap bounds the work."""
    # Make every page yield one fresh id so dedup-stop doesn't fire.
    responses = [
        _StubResponse(json_data=_wrap([_make_item(id=i)]))
        for i in range(1, 50)
    ]
    session = _install_session(monkeypatch, responses)
    jobs = KariyerScraper("any", max_pages=3).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]
    assert len(session.posts) == 3


# --- Retry / failure behaviour ------------------------------------------


def test_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 (rate-limit) is transient — back off and try again."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=429, text="slow down"),
            _StubResponse(json_data=_wrap([_make_item()])),
            _StubResponse(json_data=_wrap([])),
        ],
    )
    jobs = KariyerScraper("any", max_pages=5).fetch()
    assert len(jobs) == 1
    # Two POSTs for page 1 (retry) + one for page 2 (empty terminator).
    assert len(session.posts) == 3


def test_first_page_5xx_after_retries_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If page 1 is unreachable, surface the failure so the operator
    notices instead of producing a silent empty scrape."""
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=500, text="kaboom"),
            _StubResponse(status_code=500, text="kaboom"),
            _StubResponse(status_code=500, text="kaboom"),
        ],
    )
    with pytest.raises(ScraperError):
        KariyerScraper("any", max_pages=5).fetch()


def test_mid_pagination_5xx_returns_partial(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx on page 5 shouldn't drop the 4 pages we already paid for.
    Log + stop iterating, surface the partial."""
    responses = [
        _StubResponse(json_data=_wrap([_make_item(id=1)])),
        _StubResponse(status_code=500, text="boom"),
        _StubResponse(status_code=500, text="boom"),
        _StubResponse(status_code=500, text="boom"),
    ]
    _install_session(monkeypatch, responses)
    with caplog.at_level(logging.WARNING):
        jobs = KariyerScraper("any", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1"]
    assert any("kariyer page=2" in r.getMessage().lower() for r in caplog.records)


def test_unexpected_4xx_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-429 4xx (e.g. 403) means the bot manager flipped on us;
    surface immediately so the operator can rotate the TLS preset."""
    _install_session(
        monkeypatch,
        [_StubResponse(status_code=403, text="blocked")],
    )
    with pytest.raises(ScraperError):
        KariyerScraper("any", max_pages=5).fetch()


def test_non_json_200_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with garbage body means the gateway is mis-routing —
    don't silently swallow it."""
    _install_session(
        monkeypatch,
        [_StubResponse(status_code=200, json_data=None, text="<html>not json")],
    )
    with pytest.raises(ScraperError):
        KariyerScraper("any", max_pages=5).fetch()


# --- Module-level helper tests ------------------------------------------


def test_absolute_url_passes_absolute_through() -> None:
    assert (
        k_mod._absolute_url("https://www.kariyer.net/is-ilani/x")
        == "https://www.kariyer.net/is-ilani/x"
    )


def test_absolute_url_prefixes_relative() -> None:
    assert (
        k_mod._absolute_url("/is-ilani/foo-1")
        == "https://www.kariyer.net/is-ilani/foo-1"
    )


def test_absolute_url_adds_leading_slash_when_missing() -> None:
    """Defensive: API has been seen to occasionally drop the leading
    slash. Don't synthesise a malformed URL like
    ``https://www.kariyer.netis-ilani/...``."""
    assert (
        k_mod._absolute_url("is-ilani/foo-1")
        == "https://www.kariyer.net/is-ilani/foo-1"
    )


def test_country_iso_handles_missing_locations() -> None:
    assert k_mod._country_iso_from_locations([]) is None
    assert k_mod._country_iso_from_locations([{}]) is None


def test_country_iso_recognises_turkey_by_id_or_name() -> None:
    by_id = k_mod._country_iso_from_locations([{"countryId": "65"}])
    by_name = k_mod._country_iso_from_locations(
        [{"countryId": "", "countryName": "Türkiye"}]
    )
    assert by_id == "TR"
    assert by_name == "TR"


def test_parse_posting_date_yyyy_mm_dd() -> None:
    assert k_mod._parse_posting_date("2026-05-12") == datetime(
        2026, 5, 12, tzinfo=UTC
    )


def test_parse_posting_date_with_time() -> None:
    assert k_mod._parse_posting_date("2026-05-07T15:41") == datetime(
        2026, 5, 7, 15, 41, tzinfo=UTC
    )


def test_parse_posting_date_returns_none_for_garbage() -> None:
    assert k_mod._parse_posting_date(None) is None
    assert k_mod._parse_posting_date("") is None
    assert k_mod._parse_posting_date("yesterday") is None
    assert k_mod._parse_posting_date(12345) is None
