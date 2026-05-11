"""Tests for the Tesla scraper.

Scope: cloakbrowser gating + ``/cua-api/apps/careers/state`` parsing.
The cloakbrowser network path is verified live, not mocked.
"""

from __future__ import annotations

import logging

import pytest

from jobhive.scrapers.tesla import TeslaScraper


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
