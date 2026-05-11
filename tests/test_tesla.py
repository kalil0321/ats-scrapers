"""Tests for the Tesla scraper.

Scope: cloakbrowser gating + ``/cua-api/apps/careers/state`` parsing
+ per-job detail description formatting. The cloakbrowser network
path is verified live, not mocked.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from jobhive.scrapers.tesla import TeslaScraper, _format_description


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


# --- _fetch_details (per-job description fetch) ----------------------


class _FakePage:
    """Minimal stand-in for the cloakbrowser Page object.

    The real page exposes an async ``evaluate`` that hands a JS source
    + arg into the renderer. We don't run JS here — we just record
    which job-id batches were dispatched and return canned per-id
    payloads."""

    def __init__(self, responses: dict[str, dict | None]) -> None:
        self._responses = responses
        self.batches: list[list[str]] = []

    async def evaluate(self, _js: str, batch: list[str]) -> list[dict]:
        self.batches.append(list(batch))
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
