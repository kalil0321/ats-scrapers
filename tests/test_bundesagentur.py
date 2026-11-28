"""Tests for the Bundesagentur v6 scraper and fail-closed contract."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import Job
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


def _single_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """One region that mirrors the whole catalogue, so a leaf that stays
    oversized inside it reaches the sort-order cover."""
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "_BUNDESLAENDER", ("Berlin",))
    monkeypatch.setattr(ba, "_FOREIGN_COUNTRIES", ())


def test_oversize_query_without_verified_cover_crashes(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    _single_region(monkeypatch)
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

    _single_region(monkeypatch)
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

    _single_region(monkeypatch)
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

    _single_region(monkeypatch)
    # The slice loses a third of its jobs, far beyond the region cover's churn
    # tolerance; widen it so the sort-order cover's own handling is tested.
    monkeypatch.setattr(ba, "REGION_COVER_TOLERANCE", 0.5)
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
            # Root, domestic count, region and first cover probe see 3 jobs.
            total = 3 if probe_number <= 4 else 2
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
    assert probe_number == 10


def _regional_catalogue(
    httpx_mock,
    monkeypatch,
    jobs: dict[str, tuple[str, ...]],
    professions: dict[str, str] | None = None,
) -> list[dict[str, list[str]]]:
    """Serve ``jobs`` (reference -> regions) with no exact facet partition.

    A region is a German state or a country abroad; a reference whose regions
    name no state is a posting located only as "Deutschland". Sorting by
    ``moddatum`` reverses the order. ``professions`` (reference -> beruf) feeds
    the ``beruf`` facet and filter. Returns the query parameters of every
    request served.
    """
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "_BUNDESLAENDER", ("Berlin", "Bayern"))
    monkeypatch.setattr(ba, "_FOREIGN_COUNTRIES", ("Österreich", "Schweiz"))
    monkeypatch.setattr(ba, "PAGE_SIZE", 2)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 2)
    abroad = {"Österreich", "Schweiz"}
    seen: list[dict[str, list[str]]] = []

    def matches(regions: tuple[str, ...], wo: str | None) -> bool:
        if wo is None:
            return True
        name = wo.removesuffix(" (Bundesland)").removesuffix(" (Land)")
        if name == "Deutschland":
            return not regions or not abroad.issuperset(regions)
        return name in regions

    def serve(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlparse(str(request.url)).query)
        seen.append(params)
        wo = params.get("wo", [None])[0]
        refs = [ref for ref, regions in jobs.items() if matches(regions, wo)]
        profession = params.get("beruf", [None])[0]
        if profession is not None:
            refs = [ref for ref in refs if (professions or {}).get(ref) == profession]
        if params.get("sort") == ["moddatum"]:
            refs.reverse()
        size = int(params["size"][0])
        page = int(params["page"][0])
        items = [
            _job(ref, f"Job {ref}")
            for ref in refs[(page - 1) * size:page * size]
        ]
        counts = Counter(
            (professions or {})[ref] for ref in refs if ref in (professions or {})
        )
        payload: dict = {
            "maxErgebnisse": len(refs),
            "facetten": {"beruf": {"counts": dict(counts)}} if counts else {},
        }
        if refs:
            payload["ergebnisliste"] = items
        return httpx.Response(200, json=payload)

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    return seen


def test_oversized_leaf_is_covered_by_region(httpx_mock, monkeypatch) -> None:
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        {
            "A": ("Berlin",),
            "B": ("Bayern",),
            "C": ("Berlin", "Bayern"),
            "D": ("Österreich",),
        },
    )

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["A", "B", "C", "D"]
    regions = {params["wo"][0] for params in requests if "wo" in params}
    assert regions == {
        "Deutschland (Land)",
        "Berlin (Bundesland)",
        "Bayern (Bundesland)",
        "Österreich (Land)",
    }
    assert not any("sort" in params for params in requests)


def _countries_queried(requests: list[dict[str, list[str]]]) -> list[str]:
    return [
        params["wo"][0]
        for params in requests
        if params.get("wo", [""])[0].endswith(" (Land)")
        and params["wo"][0] != "Deutschland (Land)"
    ]


def test_region_cover_skips_countries_when_leaf_is_domestic(
    httpx_mock, monkeypatch
) -> None:
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        {"A": ("Berlin",), "B": ("Bayern",), "C": ("Bayern",)},
    )

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["A", "B", "C"]
    assert _countries_queried(requests) == []


def test_region_cover_fills_nationwide_postings_from_sort_orders(
    httpx_mock, monkeypatch
) -> None:
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        {"A": ("Berlin",), "B": ("Bayern",), "nationwide": ()},
    )

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["A", "B", "nationwide"]
    sorts = [params["sort"][0] for params in requests if "sort" in params]
    assert sorts == ["relevanz", "veroeffdatum", "moddatum"]


def test_region_cover_skips_countries_for_deutschland_only_postings(
    httpx_mock, monkeypatch
) -> None:
    """A posting located only as "Deutschland" counts as domestic, so the
    states leaving it out does not send the cover abroad."""
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        {"A": ("Berlin",), "B": ("Bayern",), "nationwide": ()},
    )

    BundesagenturScraper("any").fetch()

    assert _countries_queried(requests) == []


# The sort orders only reach the first and last page, so the middle
# posting stays out of every query.
_UNREACHABLE_MIDDLE = {
    "A": ("Berlin",),
    "B": ("Bayern",),
    "nationwide": (),
    "C": ("Berlin",),
    "D": ("Bayern",),
}


def test_region_cover_finds_postings_beyond_the_sort_orders_by_facet(
    httpx_mock, monkeypatch
) -> None:
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        _UNREACHABLE_MIDDLE,
        professions={"nationwide": "Verkäufer/in"},
    )

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == [
        "A", "B", "C", "D", "nationwide",
    ]
    assert [params["beruf"][0] for params in requests if "beruf" in params] == [
        "Verkäufer/in"
    ]


def test_region_cover_missing_postings_fails(httpx_mock, monkeypatch) -> None:
    _regional_catalogue(httpx_mock, monkeypatch, _UNREACHABLE_MIDDLE)

    with pytest.raises(ScraperError, match="regions covered 4 of 5 jobs"):
        BundesagenturScraper("any").fetch()


def test_region_cover_tolerates_churn_within_limit(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "REGION_COVER_TOLERANCE", 0.2)
    requests = _regional_catalogue(
        httpx_mock,
        monkeypatch,
        _UNREACHABLE_MIDDLE,
        professions={"nationwide": "Verkäufer/in"},
    )

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["A", "B", "C", "D"]
    assert not any("beruf" in params for params in requests)


def _end_count(monkeypatch: pytest.MonkeyPatch, end_total: int) -> None:
    """Answer the region cover's closing count of the whole slice with
    ``end_total``; the root count is the first such probe."""
    import ats_scrapers.scrapers.bundesagentur as ba

    probes = 0
    fetch_page = ba.BundesagenturScraper._fetch_page

    async def counted(self, client, sem, *, params):
        nonlocal probes
        if params.get("size") == 1 and "wo" not in params:
            probes += 1
            if probes == 2:
                return {"maxErgebnisse": end_total, "ergebnisliste": []}
        return await fetch_page(self, client, sem, params=params)

    monkeypatch.setattr(ba.BundesagenturScraper, "_fetch_page", counted)


def test_region_cover_rejects_a_slice_count_that_moved(
    httpx_mock, monkeypatch
) -> None:
    _regional_catalogue(httpx_mock, monkeypatch, _UNREACHABLE_MIDDLE)
    _end_count(monkeypatch, 0)

    with pytest.raises(ScraperError, match="changed from 5 to 0 jobs"):
        BundesagenturScraper("any").fetch()


def test_region_cover_accepts_a_slice_count_moved_within_tolerance(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "REGION_COVER_TOLERANCE", 0.2)
    _regional_catalogue(httpx_mock, monkeypatch, _UNREACHABLE_MIDDLE)
    _end_count(monkeypatch, 4)

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["A", "B", "C", "D"]


def test_empty_payload_without_list_is_retried_outside_regions(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url=_API_RE, json={"maxErgebnisse": 0}, is_reusable=True
    )

    with pytest.raises(ScraperError, match="invalid 200 payload"):
        BundesagenturScraper("any").fetch()


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


# --- Refused requests: cool off and resume the same page --------------------


def _three_pages_refusing(
    httpx_mock,
    monkeypatch: pytest.MonkeyPatch,
    refusals: dict[int, int],
    *,
    error: type[httpx.TransportError] | None = None,
) -> Counter[int]:
    """Serve three one-row pages. Page ``n`` is refused ``refusals[n]``
    times (403, or ``error`` raised) before it is served. Returns the
    number of requests per page."""
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 1)
    requests: Counter[int] = Counter()

    def serve(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        requests[page] += 1
        if requests[page] <= refusals.get(page, 0):
            if error is not None:
                raise error("refused", request=request)
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "ergebnisliste": [_job(str(page), f"Page-{page} row")],
                "maxErgebnisse": 3,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    return requests


def test_refused_page_resumes_after_cool_off(
    httpx_mock, monkeypatch, caplog
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_BLOCKED_ATTEMPTS", 6)
    requests = _three_pages_refusing(httpx_mock, monkeypatch, {2: 5})
    scraper = BundesagenturScraper("any")

    with caplog.at_level("WARNING", logger=ba.__name__):
        jobs = scraper.fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2", "3"]
    assert requests[2] == 6
    assert scraper.transient_failures == {"403": 5}
    assert "retried 5 transient failures" in caplog.text


def test_page_refused_for_every_attempt_fails_closed(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_BLOCKED_ATTEMPTS", 6)
    requests = _three_pages_refusing(httpx_mock, monkeypatch, {2: 6})

    with pytest.raises(ba._PageFetchExhaustedError, match=r"403 .* after 6 attempts"):
        BundesagenturScraper("any").fetch()
    assert requests[2] == 6


def test_network_errors_are_retried_like_refusals(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_BLOCKED_ATTEMPTS", 6)
    _three_pages_refusing(
        httpx_mock, monkeypatch, {2: 4}, error=httpx.ConnectError
    )
    scraper = BundesagenturScraper("any")

    jobs = scraper.fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2", "3"]
    assert scraper.transient_failures == {"ConnectError": 4}


def test_refusals_beyond_the_run_budget_fail_closed(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_TRANSIENT_FAILURES", 3)
    # Every page recovers on its own, but the run as a whole meets four.
    _three_pages_refusing(httpx_mock, monkeypatch, {2: 2, 3: 2})

    with pytest.raises(ba._PageFetchExhaustedError, match="4 transient failures"):
        BundesagenturScraper("any").fetch()


def test_refused_page_does_not_hold_a_request_slot(
    httpx_mock, monkeypatch
) -> None:
    """With one request slot, a slice refused until another slice is served
    only recovers if its cool-off leaves the slot free."""
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_CONCURRENCY", 1)
    monkeypatch.setattr(ba, "PAGE_SIZE", 2)
    monkeypatch.setattr(ba, "PAGE_LIMIT", 1)
    monkeypatch.setattr(ba, "PAGINATION_CAP", 2)
    served: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlparse(str(request.url)).query)
        bucket = params.get("befristung", [""])[0]
        if bucket == "1" and "2" not in served:
            return httpx.Response(403)
        served.append(bucket)
        if not bucket:
            return httpx.Response(
                200,
                json={
                    "ergebnisliste": [_job("1-a", "Probe")],
                    "maxErgebnisse": 4,
                    "facetten": {"befristung": {"counts": {"1": 2, "2": 2}}},
                },
            )
        return httpx.Response(
            200,
            json={
                "ergebnisliste": [
                    _job(f"{bucket}-a", "Job a"),
                    _job(f"{bucket}-b", "Job b"),
                ],
                "maxErgebnisse": 2,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1-a", "1-b", "2-a", "2-b"]
    assert served.index("2") < served.index("1")


def test_refusal_backoff_is_capped_and_lasts_minutes(monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 2.0)
    waits = [ba._retry_delay(n) for n in range(1, ba.MAX_BLOCKED_ATTEMPTS)]

    assert max(waits) == ba.RETRY_MAX_DELAY
    assert 15 * 60 <= sum(waits) <= 25 * 60
    assert ba._retry_delay(1, retry_after="30") == 30.0


def test_jittered_wait_stays_within_the_cap(monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 2.0)
    monkeypatch.setattr(ba, "RETRY_JITTER", 0.5)
    monkeypatch.setattr(ba.random, "uniform", lambda low, high: high)

    assert ba._retry_delay(1) == 6.0
    assert ba._retry_delay(ba.MAX_BLOCKED_ATTEMPTS) == ba.RETRY_MAX_DELAY
    assert ba._retry_delay(1, retry_after="3600") == ba.RETRY_MAX_DELAY


# --- Proxy routes: a refused route rests, the next one carries on -----------


def test_refused_route_rests_while_the_next_route_carries_on(
    httpx_mock, monkeypatch
) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 1)
    routes: list[tuple[int, int]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        route = request.extensions["route"]
        routes.append((route, page))
        if route == 0 and page == 2:
            return httpx.Response(403)
        if route == 1:
            raise httpx.ConnectError("proxy down", request=request)
        return httpx.Response(
            200,
            json={
                "ergebnisliste": [_job(str(page), f"Page-{page} row")],
                "maxErgebnisse": 3,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    scraper = BundesagenturScraper(
        "any", proxies=[None, "http://a.example:3128", "http://b.example:3128"]
    )

    jobs = scraper.fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2", "3"]
    assert scraper.transient_failures == {"403": 1, "ConnectError": 1}
    refused = routes.index((0, 2))
    assert routes[refused + 1:] == [(1, 2), (2, 2), (2, 3)]


class _BodyThatBreaksOff(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    async def __aiter__(self):
        yield b'{"ergebnisliste": ['
        raise httpx.ReadTimeout("body stalled", request=self._request)


def test_route_that_fails_mid_body_rests(httpx_mock, monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "MAX_BLOCKED_ATTEMPTS", 3)
    routes: list[int] = []

    def serve(request: httpx.Request) -> httpx.Response:
        routes.append(request.extensions["route"])
        if request.extensions["route"] == 0:
            return httpx.Response(200, stream=_BodyThatBreaksOff(request))
        return httpx.Response(
            200,
            json={"ergebnisliste": [_job("1", "Probe")], "maxErgebnisse": 1},
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    scraper = BundesagenturScraper(
        "any", proxies=[None, "http://a.example:3128"]
    )

    jobs = scraper.fetch()

    assert [job.ats_id for job in jobs] == ["1"]
    assert routes == [0, 1, 1]
    assert scraper.transient_failures == {"ReadTimeout": 1}


def test_routes_prefer_the_first_one_not_resting(monkeypatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    clock = [1_000.0]
    monkeypatch.setattr(ba.time, "monotonic", lambda: clock[0])
    routes = ba._Routes([None, "http://a.example:3128", "http://b.example:3128"])

    assert routes._pick() == 0
    routes._rest(0)
    assert routes._pick() == 1
    clock[0] += 10
    routes._rest(1)
    clock[0] += 10
    routes._rest(2)
    # All resting: the route whose rest ends first.
    assert routes._pick() == 0
    clock[0] = 1_000.0 + ba.ROUTE_COOL_OFF
    assert routes._pick() == 0


def test_without_proxies_requests_go_direct(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json={"ergebnisliste": [_job("1", "Probe")], "maxErgebnisse": 1},
        is_reusable=True,
    )

    BundesagenturScraper("any").fetch()

    assert all("route" not in r.extensions for r in httpx_mock.get_requests())


# --- Slices that change while they are paged --------------------------------


def _shrinking_slice(httpx_mock, monkeypatch, *, shrinks_at: int) -> Counter[int]:
    """Three one-row pages, of which the last closes once page ``shrinks_at``
    is requested. Past the end, the API answers without a list."""
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 1)
    requests: Counter[int] = Counter()

    def serve(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        requests[page] += 1
        total = 2 if page >= shrinks_at else 3
        if page > total:
            return httpx.Response(200, json={"maxErgebnisse": total})
        return httpx.Response(
            200,
            json={
                "ergebnisliste": [_job(str(page), f"Page-{page} row")],
                "maxErgebnisse": total,
            },
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    return requests


def test_slice_that_shrinks_while_paged_stops_at_its_new_end(
    httpx_mock, monkeypatch
) -> None:
    requests = _shrinking_slice(httpx_mock, monkeypatch, shrinks_at=2)

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2"]
    assert 3 not in requests


def test_page_past_the_new_end_is_the_last_page_not_an_error(
    httpx_mock, monkeypatch
) -> None:
    requests = _shrinking_slice(httpx_mock, monkeypatch, shrinks_at=3)

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2"]
    assert requests[3] == 1


def _slice_with_an_empty_page(
    httpx_mock, monkeypatch, *, empty_answers: int
) -> Counter[int]:
    """Three one-row pages. Page 2 comes back with an empty list, while the
    total still counts its row, for its first ``empty_answers`` requests."""
    import ats_scrapers.scrapers.bundesagentur as ba

    monkeypatch.setattr(ba, "PAGE_SIZE", 1)
    requests: Counter[int] = Counter()

    def serve(request: httpx.Request) -> httpx.Response:
        page = int(parse_qs(urlparse(str(request.url)).query)["page"][0])
        requests[page] += 1
        empty = page == 2 and requests[page] <= empty_answers
        rows = [] if empty else [_job(str(page), f"Page-{page} row")]
        return httpx.Response(
            200, json={"ergebnisliste": rows, "maxErgebnisse": 3}
        )

    httpx_mock.add_callback(serve, url=_API_RE, is_reusable=True)
    return requests


def test_empty_page_before_the_end_is_retried(httpx_mock, monkeypatch) -> None:
    requests = _slice_with_an_empty_page(httpx_mock, monkeypatch, empty_answers=1)

    jobs = BundesagenturScraper("any").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2", "3"]
    assert requests[2] == 2


def test_page_that_stays_empty_before_the_end_fails_closed(
    httpx_mock, monkeypatch
) -> None:
    _slice_with_an_empty_page(httpx_mock, monkeypatch, empty_answers=99)

    with pytest.raises(ScraperError, match="invalid 200 payload"):
        BundesagenturScraper("any").fetch()


@pytest.mark.parametrize("region", [{}, {"wo": "Niedersachsen (Bundesland)"}])
def test_payload_without_list_past_the_end_is_empty(region) -> None:
    from ats_scrapers.scrapers.bundesagentur import _checked_payload

    past_the_end = httpx.Response(200, json={"maxErgebnisse": 1583})
    params = {**region, "size": 100}

    assert _checked_payload(past_the_end, {**params, "page": 17}) == {
        "maxErgebnisse": 1583,
        "ergebnisliste": [],
    }
    with pytest.raises(ScraperError, match="missing ergebnisliste"):
        _checked_payload(past_the_end, {**params, "page": 16})


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
    with pytest.raises(ScraperError) as exc_info:
        BundesagenturScraper("any").fetch()
    assert type(exc_info.value) is ScraperError


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
# few GB of Job objects in memory, which is tight on an 8 GB server
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


@pytest.mark.parametrize(("terms", "expected"), [
    ({"vertragsdauer": "BEFRISTET", "befristungInMonaten": 24}, "fixed_term"),
    ({"vertragsdauer": "UNBEFRISTET", "istArbeitnehmerUeberlassung": True}, "permanent"),
    ({"stellenangebotsart": "AUSBILDUNG", "ausbildungsart": "AUSBILDUNG"}, "apprenticeship"),
    ({"stellenangebotsart": "SELBSTAENDIGKEIT"}, "independent_contractor"),
    ({"stellenangebotsart": "PRAKTIKUM_TRAINEE"}, None),
    ({"istArbeitnehmerUeberlassung": True, "vertragsdauer": "KEINE_ANGABE"}, None),
])
def test_ba_preserves_contract_codes_independently_of_hours(terms, expected):
    job = BundesagenturScraper("any")._parse({**_job("1", "Probe"), **terms})
    assert job is not None and job.engagement_type == expected
    assert job.workload_percentages is None
    assert terms.items() <= job.raw.items()


def test_ba_existing_detail_call_enriches_original_job(httpx_mock):
    scraper = BundesagenturScraper("any")
    job = scraper._parse({**_job("123", "Probe"), "stellenangebotsBeschreibung": None})
    httpx_mock.add_response(json={"vertragsdauer": "BEFRISTET", "befristungInMonaten": 12,
                                  "stellenangebotsBeschreibung": "Build things."})
    assert scraper.get_description(job) == "Build things."
    assert job.engagement_type == "fixed_term"
    assert job.raw["befristungInMonaten"] == 12
    assert len(httpx_mock.get_requests()) == 1


# --- Detail reads ------------------------------------------------------------

_DETAIL_RE = re.compile(
    r"^https://rest\.arbeitsagentur\.de/jobboerse/jobsuche-service/pc/v4/jobdetails/"
)


def _listed(refnr: str) -> Job:
    job = BundesagenturScraper("any")._parse(
        {**_job(refnr, "Probe"), "stellenangebotsBeschreibung": None}
    )
    assert job is not None and job.description is None
    return job


def test_detail_refused_on_one_route_is_read_through_the_next(httpx_mock) -> None:
    routes: list[int] = []

    def serve(request: httpx.Request) -> httpx.Response:
        routes.append(request.extensions["route"])
        if request.extensions["route"] == 0:
            return httpx.Response(403)
        return httpx.Response(200, json={"stellenangebotsBeschreibung": "Build things."})

    httpx_mock.add_callback(serve, url=_DETAIL_RE, is_reusable=True)
    scraper = BundesagenturScraper("any", proxies=[None, "http://a.example:3128"])

    assert scraper.get_description(_listed("1")) == "Build things."
    # The refused route rests for the next posting too.
    assert scraper.get_description(_listed("2")) == "Build things."
    assert routes == [0, 1, 1]


def test_detail_refused_every_time_raises(httpx_mock) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba

    httpx_mock.add_response(url=_DETAIL_RE, status_code=429, is_reusable=True)

    with pytest.raises(ScraperError, match="refused the detail of 1"):
        BundesagenturScraper("any").get_description(_listed("1"))
    assert len(httpx_mock.get_requests()) == ba.DETAIL_ATTEMPTS


def test_detail_network_error_is_retried(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.RemoteProtocolError("closed"), url=_DETAIL_RE)
    httpx_mock.add_response(url=_DETAIL_RE, json={"stellenangebotsBeschreibung": "Text."})

    assert BundesagenturScraper("any").get_description(_listed("1")) == "Text."


def test_gone_posting_has_no_description(httpx_mock) -> None:
    httpx_mock.add_response(url=_DETAIL_RE, status_code=404)

    assert BundesagenturScraper("any").get_description(_listed("1")) is None
    assert len(httpx_mock.get_requests()) == 1


def test_detail_fills_what_only_the_detail_carries(httpx_mock) -> None:
    job = _listed("1")
    job.is_remote = None
    httpx_mock.add_response(
        url=_DETAIL_RE,
        json={
            "stellenangebotsBeschreibung": "  Build things.  ",
            "geforderterBildungsabschluss": "MITTLERE_REIFE_MITTLERER_BILDUNGSABSCHLUSS",
            "homeofficemoeglich": True,
            "externeURL": "https://jobs.example.com/1",
            "allianzpartnerName": "jobs.example.com",
            "istPrivateArbeitsvermittlung": False,
            "befristetBis": "2027-06-30",
        },
    )

    assert BundesagenturScraper("any").get_description(job) == "Build things."
    assert [(r.label, r.requirement) for r in job.education_requirements] == [
        ("Mittlere Reife/Mittlerer Bildungsabschluss", "required")
    ]
    assert job.is_remote is True
    assert str(job.apply_url) == "https://jobs.example.com/1"
    assert job.raw["allianzpartnerName"] == "jobs.example.com"
    assert job.raw["istPrivateArbeitsvermittlung"] is False
    assert job.raw["befristetBis"] == "2027-06-30"


@pytest.mark.parametrize("code", ["NICHT_RELEVANT", None])
def test_detail_without_a_school_qualification(httpx_mock, code) -> None:
    job = _listed("1")
    httpx_mock.add_response(
        url=_DETAIL_RE,
        json={"stellenangebotsBeschreibung": "Text.", "geforderterBildungsabschluss": code},
    )

    BundesagenturScraper("any").get_description(job)

    assert job.education_requirements is None


def test_details_share_a_kept_connection(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_DETAIL_RE, json={"stellenangebotsBeschreibung": "Text."}, is_reusable=True
    )
    scraper = BundesagenturScraper("any")

    scraper.get_description(_listed("1"))
    client = scraper._detail_client(0)
    scraper.get_description(_listed("2"))

    assert scraper._detail_client(0) is client
    scraper.close()
    assert client.is_closed


def test_detail_with_a_malformed_apply_link_keeps_its_text(httpx_mock) -> None:
    job = _listed("1")
    httpx_mock.add_response(
        url=_DETAIL_RE,
        json={"stellenangebotsBeschreibung": "Text.", "externeURL": "http://bad host/"},
    )

    assert BundesagenturScraper("any").get_description(job) == "Text."
    assert job.apply_url is None


def test_direct_detail_route_ignores_the_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://env.example:3128")
    scraper = BundesagenturScraper("any", proxies=["http://a.example:3128", None])
    url = httpx.URL("https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/x")

    direct = scraper._detail_client(1)

    assert direct._transport_for_url(url) is direct._transport
    scraper.close()
