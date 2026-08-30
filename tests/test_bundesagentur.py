"""Tests for the Bundesagentur v6 scraper and fail-closed contract."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.scrapers import BundesagenturScraper

_API_RE = re.compile(
    r"^https://rest\.arbeitsagentur\.de/jobboerse/jobsuche-service/pc/v6/jobs"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba
    monkeypatch.setattr(ba, "MAX_RETRIES", 2)
    monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(ba, "RETRY_JITTER", 0.0)


def _job(refnr: str, titel: str, ort: str | None = None) -> dict:
    return {
        "referenznummer": refnr,
        "stellenangebotsTitel": titel,
        "stellenangebotsBeschreibung": "Build public services.",
        "stellenlokationen": [
            {
                "adresse": {
                    "plz": "10115",
                    "ort": ort or "Berlin",
                    "region": "BERLIN",
                    "land": "DEUTSCHLAND",
                },
                "breite": 52.532,
                "laenge": 13.384,
            }
        ],
        "firma": "ACME",
        "datumErsteVeroeffentlichung": "2026-05-01",
        "berufsfeld": "IT",
        "hauptberuf": "Softwareentwicklung und Programmierung",
        "arbeitszeitVollzeit": True,
        "homeofficemoeglich": False,
        "verguetungsangabe": "JAHRESGEHALT",
        "gehaltsspanneVon": 50_000,
        "gehaltsspanneBis": 70_000,
    }


# --- Happy path -------------------------------------------------------------


def test_simple_run_under_pagination_cap(httpx_mock) -> None:
    """A small dataset (≤10k) just paginates and returns everything."""
    httpx_mock.add_response(
        url=_API_RE,
        json={"ergebnisliste": [_job("1", "Probe")], "maxErgebnisse": 1},
        is_reusable=True,
    )
    jobs = BundesagenturScraper("any").fetch()
    assert {j.ats_id for j in jobs} == {"1"}
    assert jobs[0].description == "Build public services."
    assert jobs[0].company == "ACME"
    assert jobs[0].location == "10115 Berlin, Berlin, Deutschland"
    assert jobs[0].country_iso == "DE"
    assert jobs[0].region == "Europe"
    assert jobs[0].department == "IT"
    assert jobs[0].employment_type == "FULL_TIME"
    assert jobs[0].salary_currency == "EUR"
    assert jobs[0].salary_period == "YEAR"
    assert jobs[0].salary_min == 50_000


def test_preserves_multiple_locations_without_ambiguous_coordinates() -> None:
    item = _job("multi", "Multi-location role")
    item["stellenlokationen"].append(
        {
            "adresse": {
                "plz": "80331",
                "ort": "München",
                "region": "BAYERN",
                "land": "DEUTSCHLAND",
            },
            "breite": 48.137,
            "laenge": 11.575,
        }
    )

    job = BundesagenturScraper("any")._parse(item)

    assert job is not None
    assert job.location == (
        "10115 Berlin, Berlin, Deutschland | "
        "80331 München, Bayern, Deutschland"
    )
    assert job.country_iso == "DE"
    assert job.region == "Europe"
    assert (job.lat, job.lon) == (None, None)
    assert len(job.raw["stellenlokationen"]) == 2


def test_partition_ignores_non_exhaustive_facets() -> None:
    from ats_scrapers.scrapers.bundesagentur import _select_partition

    payload = {
        "facetten": {
            "angebotsart": {"counts": {"1": 9, "4": 1}},
            "befristung": {"counts": {"1": 6, "2": 4}},
            "berufsfeld": {"counts": {"IT": 6, "Sales": 3}},
        }
    }

    assert _select_partition(payload, total=10, applied=set()) == (
        "befristung",
        {"1": 6, "2": 4},
    )


def test_partition_uses_exact_profession_buckets() -> None:
    from ats_scrapers.scrapers.bundesagentur import _select_partition

    payload = {
        "facetten": {
            "beruf": {
                "counts": {
                    "Kaufmann/-frau - Einzelhandel": 8_000,
                    "Verkäufer/in": 4_000,
                }
            },
            "arbeitszeit": {"counts": {"vz": 11_900, "tz": 300}},
        }
    }

    assert _select_partition(payload, total=12_000, applied=set()) == (
        "beruf",
        {
            "Kaufmann/-frau - Einzelhandel": 8_000,
            "Verkäufer/in": 4_000,
        },
    )


def test_oversize_query_without_verified_cover_crashes(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 1)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 1)
    httpx_mock.add_response(
        url=_API_RE,
        json={
            "ergebnisliste": [_job("1", "Probe")],
            "maxErgebnisse": 2,
            "facetten": {},
        },
        is_reusable=True,
    )

    with pytest.raises(ScraperError, match="count-complete covers"):
        BundesagenturScraper("any").fetch()


def test_oversize_query_uses_verified_overlapping_cover(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 2)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 2)

    def serve(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlparse(str(request.url)).query)
        size = int(params.get("size", ["1"])[0])
        additional_profession = params.get("weitereberufe", [None])[0]
        if size == 1:
            items = [_job("A", "Job A")]
        elif additional_profession == "missing-tail":
            items = [_job("C", "Job C")]
        else:
            items = [_job("A", "Job A"), _job("B", "Job B")]
        return httpx.Response(
            200,
            json={
                "ergebnisliste": items,
                "maxErgebnisse": 3,
                "facetten": {
                    "arbeitsort": {
                        "counts": {"common": 2, "missing-tail": 1}
                    },
                    "weitereberufe": {
                        "counts": {"common": 2, "missing-tail": 1}
                    },
                },
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    jobs = BundesagenturScraper("any").fetch()

    assert {job.ats_id for job in jobs} == {"A", "B", "C"}


def test_verified_cover_retries_catalogue_churn(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 2)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 2)
    pass_number = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal pass_number
        params = parse_qs(urlparse(str(request.url)).query)
        size = int(params.get("size", ["1"])[0])
        sort = params.get("sort", [None])[0]
        location = params.get("arbeitsort", [None])[0]
        if size == 1:
            items = [_job("B", "Job B")]
        else:
            if sort == "relevanz":
                pass_number += 1
            if pass_number == 1:
                items = (
                    [_job("C", "Job C")]
                    if location == "missing-tail"
                    else [_job("A", "Job A"), _job("B", "Job B")]
                )
            else:
                items = (
                    [_job("D", "Job D")]
                    if location == "missing-tail"
                    else [_job("B", "Job B"), _job("C", "Job C")]
                )
        return httpx.Response(
            200,
            json={
                "ergebnisliste": items,
                "maxErgebnisse": 3,
                "facetten": {
                    "arbeitsort": {
                        "counts": {"common": 2, "missing-tail": 1}
                    }
                },
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    jobs = BundesagenturScraper("any").fetch()

    assert {job.ats_id for job in jobs} == {"B", "C", "D"}
    assert pass_number == 2


def test_verified_cover_retries_catalogue_shrink(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 2)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 2)
    probe_number = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal probe_number
        params = parse_qs(urlparse(str(request.url)).query)
        size = int(params.get("size", ["1"])[0])
        if size == 1:
            probe_number += 1
            total = 3 if probe_number <= 2 else 2
            items = [_job("A", "Job A")]
        else:
            total = 2
            items = [_job("A", "Job A"), _job("B", "Job B")]
        return httpx.Response(
            200,
            json={
                "ergebnisliste": items,
                "maxErgebnisse": total,
                "facetten": {},
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    jobs = BundesagenturScraper("any").fetch()

    assert {job.ats_id for job in jobs} == {"A", "B"}
    assert probe_number == 7


def test_missing_result_total_is_a_contract_break(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json={"ergebnisliste": [_job("1", "Probe")]},
        is_reusable=True,
    )

    with pytest.raises(ScraperError, match="valid maxErgebnisse"):
        BundesagenturScraper("any").fetch()


# --- Retry exhaustion: abort rather than publish a partial catalogue --------


def test_root_probe_persistent_403_crashes(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=403, is_reusable=True)
    with pytest.raises(ScraperError, match="returned 403"):
        BundesagenturScraper("any").fetch()


def test_probe_500_after_retries_crashes(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError, match="returned 500"):
        BundesagenturScraper("any").fetch()


# --- Page failure: abort rather than omit one page --------------------------


def test_page_failure_crashes(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba
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
                "ergebnisliste": [_job(str(page), f"Page-{page} row")],
                "maxErgebnisse": 3,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="returned 403"):
        BundesagenturScraper("any").fetch()


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
    from ats_scrapers.exceptions import ScraperError
    httpx_mock.add_response(url=_API_RE, status_code=401, is_reusable=True)
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


def test_root_probe_404_crashes_not_skips(httpx_mock) -> None:
    """Same contract for 404 — endpoint moved / decommissioned should
    crash, not silently produce ``[]``."""
    from ats_scrapers.exceptions import ScraperError
    httpx_mock.add_response(url=_API_RE, status_code=404, is_reusable=True)
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


def test_malformed_json_crashes_not_skips(httpx_mock) -> None:
    """Repeated malformed 200 responses must fail closed."""
    from ats_scrapers.exceptions import ScraperError
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        content=b"<html>Maintenance</html>",
        is_reusable=True,
    )
    with pytest.raises(ScraperError):
        BundesagenturScraper("any").fetch()


def test_incomplete_200_retries_then_succeeds(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        json={"maxErgebnisse": 1},
    )
    httpx_mock.add_response(
        url=_API_RE,
        status_code=200,
        json={"ergebnisliste": [_job("1", "Probe")], "maxErgebnisse": 1},
        is_reusable=True,
    )

    jobs = BundesagenturScraper("any").fetch()

    assert {job.ats_id for job in jobs} == {"1"}


# ---------------------------------------------------------------------------
# Streaming mode — :meth:`afetch(on_job=...)` and :meth:`fetch_stream`
# ---------------------------------------------------------------------------
#
# At ~750 k jobs the legacy list-accumulating ``afetch`` holds a
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
    """``afetch(on_job=cb)`` must call ``cb`` for every parsed
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

    out = asyncio.run(scraper.afetch(on_job=collect))
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
    out = asyncio.run(scraper.afetch())
    assert [j.ats_id for j in out] == ["R1", "R2"]


def test_fetch_stream_yields_same_jobs_as_legacy(monkeypatch) -> None:
    """``fetch_stream()`` is an async-iterator façade over
    ``afetch`` — every job legacy ``afetch`` would have
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


def test_pipeline_config_fails_closed_without_bulk_detail_requests() -> None:
    from scripts.run_pipeline import CONFIGS

    config = CONFIGS["bundesagentur"]
    assert config["fail_closed_on_empty"] is True
    assert config["skip_description_enrichment"] is True
