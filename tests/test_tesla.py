"""Tests for the Tesla scraper.

Scope: cloakbrowser gating + ``/cua-api/apps/careers/state`` parsing
+ per-job detail description formatting. The cloakbrowser network
path is verified live, not mocked.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from jobhive.scrapers.tesla import (
    TeslaScraper,
    _format_description,
    _html_to_text,
)


def test_returns_empty_with_warning_when_cloakbrowser_missing(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """When ``cloakbrowser`` isn't installed, the scraper degrades
    gracefully — logs a warning and returns ``[]`` so a publish run
    keeps moving (per the optional-browser-fallback contract)."""
    from jobhive.scrapers import _cloakbrowser

    monkeypatch.setattr(_cloakbrowser, "is_enabled", lambda: False)
    with caplog.at_level(logging.WARNING):
        jobs = TeslaScraper("tesla").fetch()
    assert jobs == []
    assert any("browser required" in r.getMessage().lower() for r in caplog.records)


def test_parses_state_payload() -> None:
    payload = {
        "listings": [
            {"id": "98765", "t": "Senior Battery Engineer", "l": "PALO_ALTO", "d": "BAT"},
            {"id": "12345", "t": "Service Technician", "l": "BERLIN_GIGAFACTORY"},
        ],
        "lookup": {
            "locations": {
                "PALO_ALTO": "Palo Alto, CA",
                "BERLIN_GIGAFACTORY": "Berlin, Germany",
            },
            "departments": {"BAT": "Energy / Battery"},
        },
    }
    jobs = TeslaScraper("tesla")._parse_payload(payload)
    assert {j.ats_id for j in jobs} == {"98765", "12345"}
    by_id = {j.ats_id: j for j in jobs}
    assert by_id["98765"].title == "Senior Battery Engineer"
    assert by_id["98765"].location == "Palo Alto, CA"
    assert by_id["98765"].department == "Energy / Battery"
    assert (
        str(by_id["98765"].url)
        == "https://www.tesla.com/careers/search/job/senior-battery-engineer-98765"
    )
    # No department in source → None propagates rather than crashing.
    assert by_id["12345"].department is None


def test_skips_entries_missing_id_or_title() -> None:
    payload = {
        "listings": [
            {"id": "1", "t": "Engineer"},
            {"t": "No id"},
            {"id": "2"},
            {},
        ],
        "lookup": {},
    }
    jobs = TeslaScraper("tesla")._parse_payload(payload)
    assert [j.ats_id for j in jobs] == ["1"]


def test_handles_unknown_location_key() -> None:
    """Tesla occasionally references a location id that's missing from
    the lookup table; surface ``None`` instead of crashing."""
    payload = {
        "listings": [{"id": "1", "t": "Engineer", "l": "UNKNOWN"}],
        "lookup": {"locations": {"PALO_ALTO": "Palo Alto, CA"}},
    }
    [job] = TeslaScraper("tesla")._parse_payload(payload)
    assert job.location is None


def test_url_slug_handles_titles_with_punctuation() -> None:
    slug = TeslaScraper._url_slug("C++ / GPU Engineer (Optimus)", "999")
    assert slug == "c-gpu-engineer-optimus-999"


# --- _format_description ---------------------------------------------


def test_format_description_concatenates_all_four_sections() -> None:
    detail = {
        "jobDescription": "Build a car",
        "jobResponsibilities": "Drive it",
        "jobRequirements": "Hands",
        "jobCompensationAndBenefits": "Equity",
    }
    out = _format_description(detail)
    # Order is fixed and matches the legacy formatter at
    # ``legacy/tesla/main.py``.
    assert out == (
        "Description:\nBuild a car\n\n"
        "Responsibilities:\nDrive it\n\n"
        "Requirements:\nHands\n\n"
        "Compensation & Benefits:\nEquity"
    )


def test_format_description_skips_missing_or_blank_sections() -> None:
    detail = {
        "jobDescription": "Body",
        "jobResponsibilities": "",       # explicit empty
        "jobRequirements": None,         # null
        "jobCompensationAndBenefits": "   ",  # whitespace-only
    }
    assert _format_description(detail) == "Description:\nBody"


def test_format_description_strips_surrounding_whitespace() -> None:
    detail = {"jobDescription": "  \n  Real text  \n  "}
    assert _format_description(detail) == "Description:\nReal text"


def test_format_description_empty_for_empty_detail() -> None:
    assert _format_description({}) == ""


def test_format_description_ignores_non_string_values() -> None:
    """Tesla occasionally ships a non-string (e.g. dict shape change);
    treat anything that isn't a non-empty string as missing rather
    than crashing the per-job loop."""
    detail = {
        "jobDescription": ["body in array"],
        "jobResponsibilities": 42,
        "jobRequirements": "Real",
    }
    assert _format_description(detail) == "Requirements:\nReal"


# --- _html_to_text ---------------------------------------------------


def test_html_to_text_strips_simple_tags() -> None:
    assert _html_to_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_html_to_text_converts_list_items_to_newlines() -> None:
    """The whole point of the helper: an ``<li>`` list must not collapse
    into a single glued line. Each ``</li>`` becomes a newline."""
    raw = "<ul><li>Lead the team</li><li>Ship code</li><li>Drink coffee</li></ul>"
    out = _html_to_text(raw)
    # All three items present, separated by newlines (order
    # preserved). Empty leading line from </ul> stripped.
    assert out.split("\n") == ["Lead the team", "Ship code", "Drink coffee"]


def test_html_to_text_unescapes_entities() -> None:
    assert _html_to_text("Salary &amp; equity &lt;&gt; bonus") == "Salary & equity <> bonus"


def test_html_to_text_collapses_excess_whitespace() -> None:
    raw = "<p>Lots   of    spaces</p>\n\n\n<p>And gaps</p>"
    out = _html_to_text(raw)
    assert out == "Lots of spaces\n\nAnd gaps"


def test_html_to_text_handles_br() -> None:
    assert _html_to_text("Line A<br>Line B<br/>Line C") == "Line A\nLine B\nLine C"


def test_html_to_text_empty_passthrough() -> None:
    assert _html_to_text("") == ""
    assert _html_to_text("   ") == ""


def test_html_to_text_unescapes_before_stripping_tags() -> None:
    """Order matters. If we stripped tags first, then unescaped, an
    encoded tag like ``&lt;script&gt;alert(1)&lt;/script&gt;`` would
    survive the strip pass and then unescape into a literal
    ``<script>...`` in the output — leaking HTML into
    ``Job.description``. Doing the unescape first means any decoded
    tags get caught by the subsequent strip."""
    raw = "Salary range &lt;script&gt;alert(1)&lt;/script&gt; 100k"
    out = _html_to_text(raw)
    # The encoded tag must be GONE — not just decoded.
    assert "<script>" not in out
    assert "</script>" not in out
    assert "<" not in out
    assert ">" not in out
    # The surrounding text and the literal payload inside the tag
    # remain (we strip tags but keep their text content).
    assert "Salary range" in out
    assert "alert(1)" in out
    assert "100k" in out


def test_format_description_strips_tesla_html_in_sections() -> None:
    """End-to-end: Tesla's real-world payload has HTML in
    Responsibilities/Requirements — the formatter must yield plain
    text without raw tags leaking through to ``Job.description``."""
    detail = {
        "jobDescription": "Be a wizard at Tesla.",
        "jobResponsibilities": "<ul><li>Build optimus</li><li>Test it</li></ul>",
        "jobRequirements": "<p>10y experience &amp; <b>passion</b></p>",
        "jobCompensationAndBenefits": None,
    }
    out = _format_description(detail)
    # No raw HTML tags anywhere.
    assert "<" not in out and ">" not in out
    # Entities decoded.
    assert "&amp;" not in out
    assert "&" in out
    # Section structure preserved.
    assert out.startswith("Description:\nBe a wizard at Tesla.")
    assert "Responsibilities:\nBuild optimus\nTest it" in out
    assert "Requirements:\n10y experience & passion" in out


# --- _fetch_details (per-job description fetch) ----------------------


class _FakePage:
    """Minimal stand-in for the cloakbrowser Page object.

    The real page exposes an async ``evaluate`` that hands a JS source
    + arg into the renderer. We don't run JS here — we just record
    which job-id batches were dispatched and return canned per-id
    payloads. The scraper now passes ``{"ids": [...], "pathTpl":
    "/cua-api/careers/job/{job_id}"}`` so the path template is the
    single source of truth (see PR #65 review comment from Greptile)."""

    def __init__(self, responses: dict[str, dict | None]) -> None:
        self._responses = responses
        self.batches: list[list[str]] = []
        self.path_tpls: list[str] = []

    async def evaluate(self, _js: str, arg: dict) -> list[dict]:
        batch = list(arg["ids"])
        self.batches.append(batch)
        self.path_tpls.append(arg["pathTpl"])
        results = []
        for job_id in batch:
            data = self._responses.get(job_id)
            if data is None:
                results.append({"id": job_id, "status": 403, "data": None})
            else:
                results.append({"id": job_id, "status": 200, "data": data})
        return results


@pytest.fixture(autouse=True)
def _fast_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the inter-batch sleep so detail tests run instantly."""
    import jobhive.scrapers.tesla as t
    monkeypatch.setattr(t, "_DETAIL_BATCH_DELAY_S", 0.0)


def test_fetch_details_batches_and_collects_successes() -> None:
    """Every successful per-id payload must land in the output dict;
    missing/blocked ids are absent (caller treats as no description)."""
    responses = {
        "1": {"jobDescription": "A"},
        "2": {"jobDescription": "B"},
        # id 3 deliberately missing → Akamai blocked / 403
    }
    page = _FakePage(responses)
    out = asyncio.run(TeslaScraper("tesla")._fetch_details(page, ["1", "2", "3"]))
    assert set(out) == {"1", "2"}
    assert out["1"]["jobDescription"] == "A"
    # All 3 ids dispatched in a single batch (concurrency=10 default).
    assert page.batches == [["1", "2", "3"]]


def test_fetch_details_empty_input_no_calls() -> None:
    page = _FakePage({})
    out = asyncio.run(TeslaScraper("tesla")._fetch_details(page, []))
    assert out == {}
    assert page.batches == []


def test_fetch_details_chunks_by_concurrency_limit() -> None:
    """A larger id list should be split into ``_DETAIL_CONCURRENCY``-sized
    batches so we don't open thousands of in-page parallel requests."""
    import jobhive.scrapers.tesla as t

    n = t._DETAIL_CONCURRENCY * 2 + 3  # 23 default → 2 full + 1 partial
    ids = [str(i) for i in range(n)]
    responses = {i: {"jobDescription": f"d-{i}"} for i in ids}
    page = _FakePage(responses)
    out = asyncio.run(TeslaScraper("tesla")._fetch_details(page, ids))
    assert len(out) == n
    assert [len(b) for b in page.batches] == [
        t._DETAIL_CONCURRENCY,
        t._DETAIL_CONCURRENCY,
        3,
    ]


def test_fetch_details_passes_path_template_to_js() -> None:
    """The path template lives in Python as a single source of truth;
    the helper hands it to ``page.evaluate`` so JS never hard-codes
    the path. Catches the drift Greptile flagged on PR #65."""
    import jobhive.scrapers.tesla as t

    page = _FakePage({"1": {"jobDescription": "x"}})
    asyncio.run(TeslaScraper("tesla")._fetch_details(page, ["1"]))
    assert page.path_tpls == [t._JOB_DETAIL_ENDPOINT]


def test_fetch_details_no_trailing_sleep_after_last_batch(monkeypatch) -> None:
    """The inter-batch sleep must fire only between batches, not after
    the final one. Catches the wasted ~300 ms tail Greptile flagged."""
    import jobhive.scrapers.tesla as t

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def tracker(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(t.asyncio, "sleep", tracker)
    # 0.0 fast-retries fixture also sets _DETAIL_BATCH_DELAY_S=0.0,
    # restore so the tracker actually records the configured delay.
    monkeypatch.setattr(t, "_DETAIL_BATCH_DELAY_S", 0.5)

    # 2 batches → exactly 1 inter-batch sleep (after batch 0).
    n = t._DETAIL_CONCURRENCY + 1
    ids = [str(i) for i in range(n)]
    page = _FakePage({i: {"jobDescription": "x"} for i in ids})
    asyncio.run(TeslaScraper("tesla")._fetch_details(page, ids))

    assert sleeps == [0.5]


def test_fetch_details_no_sleep_at_all_for_single_batch(monkeypatch) -> None:
    """Single batch → zero inter-batch sleeps. The last-batch optimization
    matters most when the catalog fits in one batch (small ATSes / mocked
    tests / smoke runs)."""
    import jobhive.scrapers.tesla as t

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def tracker(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(t.asyncio, "sleep", tracker)
    monkeypatch.setattr(t, "_DETAIL_BATCH_DELAY_S", 0.5)

    ids = [str(i) for i in range(t._DETAIL_CONCURRENCY)]  # exactly fits 1 batch
    page = _FakePage({i: {"jobDescription": "x"} for i in ids})
    asyncio.run(TeslaScraper("tesla")._fetch_details(page, ids))
    assert sleeps == []


def test_fetch_details_swallows_whole_batch_exception(caplog) -> None:
    """If the page itself raises during ``evaluate`` (browser crash,
    cookie wipe, …), the helper must keep going on later batches
    rather than abort the whole description pass."""
    import jobhive.scrapers.tesla as t

    class _FlakyPage(_FakePage):
        def __init__(self, responses, fail_on_batch_index):
            super().__init__(responses)
            self._fail_at = fail_on_batch_index
            self._call_count = 0

        async def evaluate(self, js, batch):
            i = self._call_count
            self._call_count += 1
            if i == self._fail_at:
                raise RuntimeError("page crashed")
            return await super().evaluate(js, batch)

    n = t._DETAIL_CONCURRENCY + 1  # forces a second batch
    ids = [str(i) for i in range(n)]
    responses = {i: {"jobDescription": f"d-{i}"} for i in ids}
    page = _FlakyPage(responses, fail_on_batch_index=0)
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(TeslaScraper("tesla")._fetch_details(page, ids))
    # First batch raised → nothing from it. Second batch yields its
    # single id.
    assert set(out) == {str(t._DETAIL_CONCURRENCY)}
    assert any("detail batch" in r.getMessage().lower() for r in caplog.records)
