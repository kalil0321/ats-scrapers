"""Fixtures for the live end-to-end suite.

These tests hit real ATS endpoints. They are skipped unless
``ATS_SCRAPERS_LIVE_E2E=1`` and carry the ``live`` marker so they can
be selected with ``pytest -m live``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _real_retry_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore real backoff pacing for live traffic.

    The suite-wide ``_no_retry_delays`` fixture zeroes the shared fetch
    delays so mocked tests run fast. Against real endpoints that would
    turn a 429 into a hammering loop — put polite pacing back.
    """
    from ats_scrapers import fetch as fetch_module

    monkeypatch.setattr(fetch_module, "DEFAULT_RETRY_BASE_DELAY", 1.5)
    monkeypatch.setattr(fetch_module, "DEFAULT_MAX_RETRY_DELAY", 30.0)
