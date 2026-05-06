"""Tests for the Bundesagentur scraper.

Focused on the failure-mode contract: probe failures must skip the affected
subtree (and shout about it), page failures must skip just one page, and
neither may silently look like a clean ``maxErgebnisse=0`` response.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jobhive.scrapers import BundesagenturScraper

_API_RE = re.compile(
    r"^https://rest\.arbeitsagentur\.de/jobboerse/jobsuche-service/pc/v4/jobs"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.bundesagentur as ba
    monkeypatch.setattr(ba, "MAX_RETRIES", 2)
    monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(ba, "RETRY_JITTER", 0.0)


def _job(refnr: str, titel: str, ort: str | None = None) -> dict:
    return {
        "refnr": refnr,
        "titel": titel,
        "arbeitsort": {"ort": ort or "Berlin", "land": "Deutschland"},
        "arbeitgeber": "ACME",
        "aktuelleVeroeffentlichungsdatum": "2026-05-01",
    }


# --- Happy path -------------------------------------------------------------


def test_simple_run_under_pagination_cap(httpx_mock) -> None:
    """A small dataset (≤10k) just paginates and returns everything."""
    httpx_mock.add_response(
        url=_API_RE,
        json={"stellenangebote": [_job("1", "Probe")], "maxErgebnisse": 1},
        is_reusable=True,
    )
    jobs = BundesagenturScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1"}


# --- Probe failure: must NOT silently look like maxErgebnisse=0 -------------


def test_root_probe_persistent_403_skips_subtree(httpx_mock, caplog) -> None:
    """If the very first probe (no facets) keeps returning 403 the entire
    scrape must NOT just return an empty list silently — that would publish
    a wholesale undercount as a successful run. We log loudly and return
    whatever was collected (here: nothing)."""
    httpx_mock.add_response(url=_API_RE, status_code=403, is_reusable=True)
    with caplog.at_level(logging.WARNING, logger="jobhive.scrapers.bundesagentur"):
        jobs = BundesagenturScraper("any").fetch()
    assert jobs == []
    # The warning must say "subtree skipped" so an operator can spot the
    # undercount in the logs. The previous soft-fail returned a fake
    # ``maxErgebnisse=0`` payload that produced no warning at all.
    assert any(
        "subtree skipped" in rec.getMessage().lower()
        for rec in caplog.records
    ), "expected 'subtree skipped' warning, got: " + "\n".join(
        rec.getMessage() for rec in caplog.records
    )


def test_probe_500_after_retries_skips_with_warning(
    httpx_mock, caplog
) -> None:
    """Same pattern for 500 — the previous code returned empty and looked
    like a clean zero-result query. Now the failure is logged."""
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with caplog.at_level(logging.WARNING, logger="jobhive.scrapers.bundesagentur"):
        jobs = BundesagenturScraper("any").fetch()
    assert jobs == []
    assert any(
        "subtree skipped" in rec.getMessage().lower()
        for rec in caplog.records
    )


# --- Page failure: skip just the page, keep going ---------------------------


def test_page_failure_logs_page_skip_not_subtree_skip(
    httpx_mock, monkeypatch, caplog
) -> None:
    """A page-level failure inside ``_fan_out_pages`` must log a *page*
    skip (bounded loss) — not a subtree skip — and must not silently
    look like a clean response.
    """
    import jobhive.scrapers.bundesagentur as ba
    # Tiny page size so a 3-row dataset spans 3 pages and we can exercise
    # the per-page failure path deterministically.
    monkeypatch.setattr(ba, "PAGE_SIZE", 1)

    def serve(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlparse(str(request.url)).query)
        page = int(params.get("page", ["1"])[0])
        # The probe (page=1) and page 1 of fan-out succeed; page 2
        # persistently 403s; page 3 succeeds.
        if page == 2:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "stellenangebote": [_job(str(page), f"Page-{page} row")],
                "maxErgebnisse": 3,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    with caplog.at_level(logging.WARNING, logger="jobhive.scrapers.bundesagentur"):
        jobs = BundesagenturScraper("any").fetch()

    # Pages 1 and 3 made it through; page 2 was lost. The leaf and the
    # subtree both kept going.
    ats_ids = {j.ats_id for j in jobs}
    assert "1" in ats_ids and "3" in ats_ids
    assert "2" not in ats_ids

    page_warnings = [
        r for r in caplog.records if "page skipped" in r.getMessage().lower()
    ]
    subtree_warnings = [
        r for r in caplog.records if "subtree skipped" in r.getMessage().lower()
    ]
    assert page_warnings, "expected page-level warning"
    assert not subtree_warnings, "page failure must NOT escalate to subtree skip"
