"""Tests for the Bayt.com (MENA) scraper.

Scope: httpcloak gating (graceful degradation when the optional
TLS-impersonation HTTP client is missing), Cloudflare-challenge
detection + preset rotation, HTML row parsing, country-resolution
fallback chain, pagination dedup, retry/backoff, and registry
wiring. The httpcloak network path is exercised via a stub session
— we never hit the live site in tests.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import BaytScraper, ScraperRegistry
from jobhive.scrapers import bayt as b_mod

# --- Stub session --------------------------------------------------------


class _StubResponse:
    """Mimics ``httpcloak.Response`` enough for the scraper's needs."""

    def __init__(self, *, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _StubSession:
    """Stand-in for ``httpcloak.Session``. Records every GET so tests
    can assert on URL + headers, then replays canned responses in
    order.

    The scraper doesn't use ``Session`` as a context manager (it
    keeps the session alive across pages and calls ``close()`` in a
    finally) so we mirror that flat call shape.
    """

    def __init__(self, responses: list[_StubResponse]) -> None:
        self._responses = list(responses)
        self.gets: list[dict[str, Any]] = []
        self.closed = False

    def get(
        self, url: str, *, headers: dict, timeout: float,
    ) -> _StubResponse:
        self.gets.append(
            {"url": url, "headers": headers, "timeout": timeout}
        )
        if not self._responses:
            raise AssertionError("ran out of stubbed responses")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps add seconds per retry — patch to a no-op so
    retry-path tests don't pay wall-clock cost."""
    monkeypatch.setattr(b_mod, "_sleep_backoff", lambda attempt: None)


def _install_session(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_StubResponse],
) -> _StubSession:
    """Pretend ``httpcloak.Session(preset=...)`` returns our stub.

    The scraper iterates over multiple presets in
    ``_open_session_and_first_page``; we surface a *single* session
    so the test scopes one preset at a time. The scraper's
    contract is "first non-CF preset wins" so the canned page 1
    response decides whether preset rotation happens.
    """
    session = _StubSession(responses)
    sessions_created: list[_StubSession] = []

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError  # type the scraper raises

        @staticmethod
        def Session(preset: str) -> _StubSession:  # noqa: N802 - mimic API
            session._preset = preset  # type: ignore[attr-defined]
            sessions_created.append(session)
            return session

    monkeypatch.setattr(b_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "httpcloak", _StubHttpCloak)
    session._sessions_created = sessions_created  # type: ignore[attr-defined]
    return session


def _install_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    responses_per_preset: list[list[_StubResponse]],
) -> list[_StubSession]:
    """Mounts a stub ``httpcloak`` that hands out a *different*
    session per ``Session(preset=...)`` call. Used to test the
    Cloudflare-preset-rotation path: the first N sessions return CF
    challenges, the N+1th clears, and the scraper continues with the
    cleared session.
    """
    sessions = [_StubSession(r) for r in responses_per_preset]
    counter = {"idx": 0}

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError

        @staticmethod
        def Session(preset: str) -> _StubSession:  # noqa: N802
            i = counter["idx"]
            if i >= len(sessions):
                raise AssertionError(
                    f"more presets tried ({i + 1}) than stubbed "
                    f"({len(sessions)})"
                )
            session = sessions[i]
            session._preset = preset  # type: ignore[attr-defined]
            counter["idx"] += 1
            return session

    monkeypatch.setattr(b_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "httpcloak", _StubHttpCloak)
    return sessions


# --- HTML fixtures ----------------------------------------------------


def _make_row(
    *,
    job_id: str = "5223155",
    title: str = "IT Audit Manager",
    href: str = "/en/saudi-arabia/jobs/it-audit-manager-5223155/",
    company: str = "Michael Page",
    location: str = "Riyadh · Saudi Arabia",
    description: str = "The IT Audit Manager will lead IT audits.",
    career_level: str = "Management",
    timestamp: int = 1734696141,
    is_aggregated: str = "0",
    is_external: str = "0",
) -> str:
    """One realistic ``<li data-js-job>`` row. Mirrors the markup
    captured from a 2024 Web Archive snapshot of the live page."""
    return f"""
<li data-js-job class="has-pointer-d" data-job-id="{job_id}">
<div class="row is-compact is-m no-wrap">
<h2 class="col u-stretch t-large m0 t-nowrap-d t-trim">
<a data-automation-is_aggregated="{is_aggregated}"
   data-automation-is_external="{is_external}"
   data-js-aid="jobID" data-js-link
   href="{href}">{title}</a>
</h2>
</div>
<div class="row is-m no-wrap">
<div class="t-nowrap p10l">
<div class="t-nowrap"><span class="t-default t-small">{company}</span></div>
<div class="t-mute t-small">{location}</div>
</div>
</div>
<div class="jb-descr m10t t-small">{description}</div>
<div class="jb-tags m10t">
<dl class="dlist is-spaced t-small m0y row">
<dt class="p0 m20r jb-label-careerlevel">{career_level}</dt>
</dl>
</div>
<div class="jb-footer row is-m v-align-center m10t">
<span data-automation-jobactivedate="{timestamp}">just now</span>
</div>
</li>
""".strip()


def _wrap_page(rows: list[str]) -> str:
    """Wrap a list of row fragments in just enough surrounding HTML
    to mirror what Bayt serves. The scraper extracts rows by
    ``<li data-js-job>`` boundary regex so the wrapper content
    doesn't matter for parsing — we keep it minimal."""
    rows_html = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en"><head><title>Jobs in Middle East</title></head>
<body>
<ul class="jobs-list">
{rows_html}
</ul>
</body></html>"""


_CLOUDFLARE_PAGE = (
    '<!DOCTYPE html><html lang="en-US"><head>'
    '<title>Just a moment...</title>'
    '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
    '</head><body>cf-challenge</body></html>'
)


# --- Registry / construction ------------------------------------------


def test_registry_resolves_bayt() -> None:
    assert ScraperRegistry.get(ATSType.BAYT) is BaytScraper


def test_default_country_slug_is_international() -> None:
    s = BaytScraper()
    assert s.country_slug == "international"
    assert s.country_iso_hint is None


@pytest.mark.parametrize(
    ("slug", "iso"),
    [
        ("uae", "AE"),
        ("saudi-arabia", "SA"),
        ("saudi", "SA"),  # alias gets canonicalised
        ("egypt", "EG"),
        ("lebanon", "LB"),
        ("qatar", "QA"),
        ("kuwait", "KW"),
        ("bahrain", "BH"),
        ("oman", "OM"),
        ("jordan", "JO"),
        ("morocco", "MA"),
    ],
)
def test_known_country_slugs_set_iso_hint(slug: str, iso: str) -> None:
    s = BaytScraper(slug)
    assert s.country_iso_hint == iso


def test_saudi_alias_canonicalises_to_saudi_arabia() -> None:
    """``saudi`` is shorthand; Bayt's URLs use ``saudi-arabia``. The
    scraper rewrites the slug so the URL builder produces the right
    path."""
    s = BaytScraper("saudi")
    assert s.country_slug == "saudi-arabia"


def test_unknown_country_slug_raises() -> None:
    with pytest.raises(ScraperError, match="unknown country slug"):
        BaytScraper("atlantis")


# --- Graceful degradation --------------------------------------------


def test_returns_empty_with_warning_when_httpcloak_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``httpcloak`` isn't installed, log a warning and return
    ``[]`` so the publish pipeline keeps moving — same contract as
    Kariyer / Tesla."""
    monkeypatch.setattr(b_mod, "_httpcloak_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        jobs = BaytScraper().fetch()
    assert jobs == []
    assert any("httpcloak required" in r.getMessage().lower() for r in caplog.records)


# --- Parsing ----------------------------------------------------------


def test_parses_minimal_realistic_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end parse of a single canonical row. Pins the field
    mapping the public dataset relies on."""
    page = _wrap_page([_make_row()])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),  # empty
        ],
    )

    jobs = BaytScraper("international", max_pages=5).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.BAYT
    assert j.ats_id == "5223155"
    assert j.title == "IT Audit Manager"
    assert j.company == "Michael Page"
    assert str(j.url) == (
        "https://www.bayt.com/en/saudi-arabia/jobs/it-audit-manager-5223155/"
    )
    assert j.location == "Riyadh · Saudi Arabia"
    # Country derived from the href because the scraper-level hint
    # is None (international slug).
    assert j.country_iso == "SA"
    assert j.region == "Asia"
    assert j.language == "en"
    assert j.posted_at == datetime.fromtimestamp(1734696141, tz=UTC)
    assert j.fetched_at is not None
    assert j.description is not None
    assert "IT Audit Manager" in j.description
    assert j.raw is not None
    assert j.raw["career_level"] == "Management"
    assert j.raw["country_slug"] == "international"
    assert j.raw["is_aggregated"] is False
    assert j.raw["is_external"] is False


def test_country_iso_uses_scraper_hint_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the constructor is called with a per-country slug
    (``uae``), the hint short-circuits the href-parsing fallback —
    every row inherits the country."""
    # Even though the href says ``saudi-arabia``, the hint wins.
    page = _wrap_page([_make_row(href="/en/saudi-arabia/jobs/x-1/")])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("uae", max_pages=5).fetch()
    assert job.country_iso == "AE"


def test_country_iso_falls_back_to_location_when_href_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the href country segment is unrecognised (a new country
    Bayt expands into?), fall back to parsing the location string."""
    page = _wrap_page(
        [
            _make_row(
                href="/en/martian-jobs/foo-99/",
                location="Cairo · Egypt",
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("international", max_pages=5).fetch()
    assert job.country_iso == "EG"
    assert job.region == "Africa"


def test_country_iso_none_when_no_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hint, no recognisable href, no recognisable location —
    leave ``country_iso`` ``None`` so LLM enrichment fills it.
    Mustn't crash."""
    page = _wrap_page(
        [
            _make_row(
                href="/en/space-station/jobs/zero-g-eng-1/",
                location="ISS · Low Earth Orbit",
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("international", max_pages=5).fetch()
    assert job.country_iso is None
    assert job.region is None


def test_external_and_aggregated_flags_surface_in_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bayt marks third-party / aggregated postings with attribute
    flags on the title anchor. We surface them in ``raw`` so the
    publish layer can filter them downstream."""
    page = _wrap_page(
        [_make_row(is_aggregated="1", is_external="1")],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("international", max_pages=5).fetch()
    assert job.raw is not None
    assert job.raw["is_aggregated"] is True
    assert job.raw["is_external"] is True


def test_skips_rows_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confidential postings sometimes ship a partial row (no title /
    no href). Skip rather than fake values."""
    valid = _make_row(job_id="1")
    no_title = (
        '<li data-js-job class="has-pointer-d" data-job-id="2">'
        '<div>nothing</div></li>'
    )
    no_anchor = (
        '<li data-js-job class="has-pointer-d" data-job-id="3">'
        '<h2>just text no link</h2></li>'
    )
    page = _wrap_page([valid, no_title, no_anchor])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = BaytScraper("international", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


def test_handles_absolute_href(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if Bayt ever returns an absolute URL instead of a
    site-relative path, don't double-prefix the host."""
    page = _wrap_page(
        [
            _make_row(
                href="https://www.bayt.com/en/uae/jobs/x-42/",
                job_id="42",
            ),
        ],
    )
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("international", max_pages=5).fetch()
    assert str(job.url) == "https://www.bayt.com/en/uae/jobs/x-42/"


def test_invalid_timestamp_falls_back_to_none_posted_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage epoch (e.g. corrupted markup) mustn't crash the
    parse — leave ``posted_at`` ``None`` and continue."""
    page = _wrap_page([_make_row(timestamp=9999999999999999)])
    _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=page),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    [job] = BaytScraper("international", max_pages=5).fetch()
    assert job.posted_at is None


# --- Pagination & dedup -----------------------------------------------


def test_paginates_until_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scraper walks pages 1..N until the response yields zero
    rows, then stops. URL ``page=`` increments each call."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200, text=_wrap_page([_make_row(job_id="1")]),
            ),
            _StubResponse(
                status_code=200, text=_wrap_page([_make_row(job_id="2")]),
            ),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = BaytScraper("international", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2"]
    pages = [g["url"] for g in session.gets]
    assert "page=1" in pages[0]
    assert "page=2" in pages[1]
    assert "page=3" in pages[2]


def test_dedupes_sticky_rows_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bayt's "Featured" sticky rows repeat on every page. They
    must surface once across the whole walk."""
    sticky = _make_row(job_id="999")
    _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([sticky, _make_row(job_id="1")]),
            ),
            _StubResponse(
                status_code=200,
                text=_wrap_page([sticky, _make_row(job_id="2")]),
            ),
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    jobs = BaytScraper("international", max_pages=5).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "2", "999"]


def test_stops_when_whole_page_is_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the tail of the catalogue every entry is a sticky repeat.
    Break instead of walking a thousand empty pages."""
    sticky = _make_row(job_id="999")
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([sticky, _make_row(job_id="1")]),
            ),
            _StubResponse(
                status_code=200, text=_wrap_page([sticky]),  # all dupes
            ),
            # Should never request a third page.
        ],
    )
    jobs = BaytScraper("international", max_pages=99).fetch()
    assert sorted(j.ats_id for j in jobs) == ["1", "999"]
    assert len(session.gets) == 2


def test_respects_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving site could keep returning fresh ids forever.
    The safety cap bounds the work."""
    responses = [
        _StubResponse(
            status_code=200,
            text=_wrap_page([_make_row(job_id=str(i))]),
        )
        for i in range(1, 50)
    ]
    session = _install_session(monkeypatch, responses)
    jobs = BaytScraper("international", max_pages=3).fetch()
    assert [j.ats_id for j in jobs] == ["1", "2", "3"]
    assert len(session.gets) == 3


def test_url_uses_country_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The country slug from the constructor is interpolated into
    the URL path so the per-country slices hit the right endpoint."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(status_code=200, text=_wrap_page([])),
        ],
    )
    BaytScraper("uae", max_pages=1).fetch()
    assert "/en/uae/jobs/" in session.gets[0]["url"]


# --- Cloudflare / preset rotation -------------------------------------


def test_rotates_presets_when_first_returns_cloudflare(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """First preset returns a CF challenge, second clears — scraper
    should use the cleared session for the rest of the walk."""
    sessions = _install_session_factory(
        monkeypatch,
        [
            # Preset 1 (chrome-latest-windows): blocked.
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            # Preset 2 (chrome-latest-macos): clears + we paginate.
            [
                _StubResponse(
                    status_code=200,
                    text=_wrap_page([_make_row(job_id="1")]),
                ),
                _StubResponse(status_code=200, text=_wrap_page([])),
            ],
        ],
    )
    with caplog.at_level(logging.INFO):
        jobs = BaytScraper("international", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]
    # Both sessions were created; the first was closed after the CF
    # block; the second is the one that surfaced jobs.
    assert sessions[0].closed is True


def test_returns_empty_when_all_presets_blocked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When every preset hits Cloudflare, log + return ``[]``.
    Graceful degradation — don't crash the pipeline."""
    sessions = _install_session_factory(
        monkeypatch,
        [
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=403, text=_CLOUDFLARE_PAGE)],
        ],
    )
    with caplog.at_level(logging.WARNING):
        jobs = BaytScraper("international", max_pages=5).fetch()
    assert jobs == []
    assert any(
        "all 5 tls presets blocked" in r.getMessage().lower()
        for r in caplog.records
    )
    # Every session should have been cleaned up.
    assert all(s.closed for s in sessions)


def test_two_hundred_status_with_cloudflare_body_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CF sometimes serves the challenge page with a 200 status (the
    JS-challenge HTML, not a 403). The scraper must detect via the
    page title, not just the status code."""
    sessions = _install_session_factory(
        monkeypatch,
        [
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)],
            [_StubResponse(status_code=200, text=_CLOUDFLARE_PAGE)],
        ],
    )
    jobs = BaytScraper("international", max_pages=5).fetch()
    assert jobs == []
    assert all(s.closed for s in sessions)


# --- Retry behaviour --------------------------------------------------


def test_retries_mid_pagination_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx on page 2 is retried; if all three retries fail we
    surface the partial from page 1, not a crash."""
    session = _install_session(
        monkeypatch,
        [
            _StubResponse(
                status_code=200,
                text=_wrap_page([_make_row(job_id="1")]),
            ),
            _StubResponse(status_code=500, text="kaboom"),
            _StubResponse(status_code=500, text="kaboom"),
            _StubResponse(status_code=500, text="kaboom"),
        ],
    )
    jobs = BaytScraper("international", max_pages=10).fetch()
    assert [j.ats_id for j in jobs] == ["1"]
    # 1 (page 1) + 3 retries on page 2 = 4 calls
    assert len(session.gets) == 4


def test_transport_error_mid_pagination_returns_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level error on page 2 must not lose page 1's
    rows. Log and return what we have."""

    class _FlakySession(_StubSession):
        def get(
            self, url: str, *, headers: dict, timeout: float,
        ) -> _StubResponse:
            self.gets.append({"url": url, "headers": headers, "timeout": timeout})
            if "page=1" in url:
                return _StubResponse(
                    status_code=200,
                    text=_wrap_page([_make_row(job_id="1")]),
                )
            raise RuntimeError("transport down")

    session = _FlakySession([])

    class _StubHttpCloak:
        HTTPCloakError = RuntimeError

        @staticmethod
        def Session(preset: str) -> _FlakySession:  # noqa: N802
            return session

    monkeypatch.setattr(b_mod, "_httpcloak_available", lambda: True)
    monkeypatch.setitem(
        __import__("sys").modules, "httpcloak", _StubHttpCloak,
    )
    jobs = BaytScraper("international", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]


# --- Module-level helper tests -----------------------------------------


def test_absolute_url_passes_absolute_through() -> None:
    assert (
        b_mod._absolute_url("https://www.bayt.com/en/uae/jobs/x")
        == "https://www.bayt.com/en/uae/jobs/x"
    )


def test_absolute_url_prefixes_relative() -> None:
    assert (
        b_mod._absolute_url("/en/uae/jobs/foo-1/")
        == "https://www.bayt.com/en/uae/jobs/foo-1/"
    )


def test_absolute_url_adds_leading_slash_when_missing() -> None:
    """Defensive: if a href ever drops the leading slash, don't
    synthesise a malformed URL like ``https://www.bayt.comen/…``."""
    assert (
        b_mod._absolute_url("en/uae/jobs/foo-1/")
        == "https://www.bayt.com/en/uae/jobs/foo-1/"
    )


def test_build_url_pattern() -> None:
    assert (
        b_mod._build_url("uae", 3)
        == "https://www.bayt.com/en/uae/jobs/?page=3"
    )


def test_resolve_country_iso_hint_wins() -> None:
    assert (
        b_mod._resolve_country_iso(
            "AE", "/en/saudi-arabia/jobs/x-1/", "Dubai · UAE",
        )
        == "AE"
    )


def test_resolve_country_iso_href_falls_through() -> None:
    assert (
        b_mod._resolve_country_iso(
            None, "/en/qatar/jobs/x-1/", None,
        )
        == "QA"
    )


def test_resolve_country_iso_location_last_resort() -> None:
    assert (
        b_mod._resolve_country_iso(
            None,
            "/en/unknown/jobs/x-1/",
            "Beirut · Lebanon",
        )
        == "LB"
    )


def test_resolve_country_iso_none_when_no_signal() -> None:
    assert (
        b_mod._resolve_country_iso(None, "/en/none/jobs/x-1/", None)
        is None
    )


def test_iter_job_rows_yields_each_row_with_id() -> None:
    page = _wrap_page(
        [
            _make_row(job_id="1"),
            _make_row(job_id="2"),
            _make_row(job_id="3"),
        ],
    )
    rows = list(b_mod._iter_job_rows(page))
    assert [r[0] for r in rows] == ["1", "2", "3"]
    # Each row body actually contains its own job-id attribute so
    # downstream parsers can re-verify if they want.
    for jid, body in rows:
        assert f'data-job-id="{jid}"' in body


def test_iter_job_rows_returns_empty_on_no_jobs() -> None:
    assert list(b_mod._iter_job_rows("<html><body>nope</body></html>")) == []


def test_region_for_iso_covers_mena() -> None:
    assert b_mod._region_for_iso("AE") == "Asia"
    assert b_mod._region_for_iso("EG") == "Africa"
    assert b_mod._region_for_iso(None) is None
    assert b_mod._region_for_iso("ZZ") is None
