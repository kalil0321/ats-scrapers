"""Tests for the Bundesagentur scraper.

Focused on the failure-mode contract: probe failures must skip the affected
subtree (and shout about it), page failures must skip just one page, and
neither may silently look like a clean ``maxErgebnisse=0`` response.
"""

from __future__ import annotations

import asyncio
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


# --- Contract-break failures: must crash, not soft-fail --------------------
#
# Codex review on #14: the broad ``except ScraperError`` in ``_exhaust_query``
# was swallowing more than just persistent WAF blocks. A 401/404 contract
# break, malformed JSON, or non-retryable 4xx all raised the same
# ``ScraperError`` — which the soft-fail handler caught and turned into a
# silent ``[]``. The scraper now distinguishes ``_PageFetchExhaustedError``
# (transient, swallowed) from plain ``ScraperError`` (contract break,
# raised). These tests pin that distinction.


def test_root_probe_401_crashes_not_skips(httpx_mock) -> None:
    """A 401 on the root probe is a contract break (auth removed / API
    moved), not a transient WAF block. The scraper must crash so an
    operator notices — silently returning ``[]`` would publish a
    wholesale undercount as a successful run."""
    from jobhive.exceptions import ScraperError
    httpx_mock.add_response(url=_API_RE, status_code=401, is_reusable=True)
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


def test_root_probe_404_crashes_not_skips(httpx_mock) -> None:
    """Same contract for 404 — endpoint moved / decommissioned should
    crash, not silently produce ``[]``."""
    from jobhive.exceptions import ScraperError
    httpx_mock.add_response(url=_API_RE, status_code=404, is_reusable=True)
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


def test_malformed_json_crashes_not_skips(httpx_mock) -> None:
    """A 200 OK with a malformed body is a contract break — the schema
    we're parsing against is unknown — and must crash rather than
    soft-fail to ``[]``."""
    from jobhive.exceptions import ScraperError
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        content=b"<html>Maintenance</html>",
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


# ---------------------------------------------------------------------------
# Streaming mode — :meth:`_fetch_async(on_job=...)` and :meth:`fetch_stream`
# ---------------------------------------------------------------------------
#
# At ~750 k jobs the legacy list-accumulating ``_fetch_async`` holds a
# few GB of Job objects in memory, which is tight on the 7.6 GB VPS
# when other scrapers are also resident. The streaming variant pushes
# each parsed Job to an async callback (or asyncio.Queue in the
# ``fetch_stream`` wrapper) instead of accumulating, leaving only the
# ``seen`` ID set in RAM (~30 MB at full scale).


def _fake_exhaust(items_to_emit):
    """Mimic ``_exhaust_query``'s contract: call ``absorb`` once with
    a single batch of items, then return."""
    async def _fake(client, sem, *, base_params, depth, absorb):
        await absorb(items_to_emit)
    return _fake


def test_on_job_callback_invoked_per_deduped_job(monkeypatch) -> None:
    """``_fetch_async(on_job=cb)`` must call ``cb`` for every parsed
    job that survives dedup, and must NOT accumulate them into the
    returned list. Dedup is by ``refnr`` / ``ats_id``."""
    scraper = BundesagenturScraper("any")
    items = [
        _job("REF-A", "Job A", ort="Berlin"),
        _job("REF-B", "Job B", ort="Munich"),
        _job("REF-A", "Job A duplicate"),  # same refnr — dropped
    ]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))

    received: list = []
    async def collect(job):
        received.append(job)

    out = asyncio.run(scraper._fetch_async(on_job=collect))
    # Streaming mode → returned list is empty.
    assert out == []
    # 2 jobs survived dedup (A, B).
    assert [j.ats_id for j in received] == ["REF-A", "REF-B"]


def test_on_job_none_accumulates_to_list(monkeypatch) -> None:
    """Without an ``on_job`` sink we keep the legacy behaviour:
    every deduped job lands in the returned list."""
    scraper = BundesagenturScraper("any")
    items = [_job("R1", "T1"), _job("R2", "T2"), _job("R1", "T1 dup")]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))
    out = asyncio.run(scraper._fetch_async())
    assert [j.ats_id for j in out] == ["R1", "R2"]


def test_fetch_stream_yields_same_jobs_as_legacy(monkeypatch) -> None:
    """``fetch_stream()`` is an async-iterator façade over
    ``_fetch_async`` — every job legacy ``_fetch_async`` would have
    returned must come out of the stream in some order."""
    scraper = BundesagenturScraper("any")
    items = [_job(f"R-{i}", f"Job {i}") for i in range(7)]
    monkeypatch.setattr(scraper, "_exhaust_query", _fake_exhaust(items))

    async def collect_stream() -> list:
        out = []
        async for job in scraper.fetch_stream():
            out.append(job)
        return out

    streamed = asyncio.run(collect_stream())
    assert sorted(j.ats_id for j in streamed) == sorted(f"R-{i}" for i in range(7))
