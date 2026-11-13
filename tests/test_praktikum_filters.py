"""Opt-in Praktikum/internship filters for Bundesagentur + EURES.

The two largest public job sources in Europe expose internship filters
that the scrapers never sent (measured live, 2026-10-02):

- Bundesagentur v6 lists an ``angebotsart`` facet whose buckets use
  NUMERIC codes: ``34`` = PRAKTIKUM_TRAINEE (14,3k live rows).
- EURES accepts ``keywords`` entries of the shape
  ``{"keyword": ..., "specificSearchCode": "EVERYWHERE"}``
  (35,486 live DE rows for "Praktikum"); a list of plain strings is
  rejected with 400.

Both are opt-in constructor kwargs: the default behaviour (full
catalogue) is byte-identical.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ats_scrapers.scrapers import BundesagenturScraper, EuresScraper

_BA_API = re.compile(
    r"^https://rest\.arbeitsagentur\.de/jobboerse/jobsuche-service/pc/v6/jobs"
)
_EURES_API = re.compile(
    r"^https://europa\.eu/eures/api/jv-searchengine/public/jv-search/search$"
)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import ats_scrapers.scrapers.bundesagentur as ba
    monkeypatch.setattr(ba, "MAX_RETRIES", 2)
    monkeypatch.setattr(ba, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(ba, "RETRY_JITTER", 0.0)


def _ba_job(refnr: str, titel: str) -> dict:
    return {
        "referenznummer": refnr,
        "stellenangebotsTitel": titel,
        "stellenangebotsBeschreibung": "Internship duties.",
        "stellenlokationen": [
            {"adresse": {"plz": "10115", "ort": "Berlin",
                         "region": "BERLIN", "land": "DEUTSCHLAND"}}
        ],
        "firma": "ACME",
    }


def _ba_response(n: int) -> dict:
    return {
        "ergebnisliste": [_ba_job(str(i), f"Praktikum {i}") for i in range(n)],
        "maxErgebnisse": n,
    }


def _eures_response(n: int) -> dict:
    return {
        "numberRecords": n,
        "jvs": [
            {"id": f"jv-{i}", "title": f"Praktikum {i}",
             "employerName": "ACME",
             "locationMap": {"DE": ["DE12"]}}
            for i in range(n)
        ],
        "facets": {},
    }


# --- Bundesagentur: default sends no filter -------------------------------


def test_ba_default_sends_no_angebotsart(httpx_mock) -> None:
    seen_params: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json=_ba_response(1))

    httpx_mock.add_callback(method="GET", url=_BA_API, callback=handler, is_reusable=True)
    jobs = BundesagenturScraper("any").fetch()
    assert len(jobs) == 1
    assert all("angebotsart" not in p for p in seen_params)


def test_ba_angebotsart_sent_on_every_request(httpx_mock) -> None:
    """The opt-in filter must reach the root probe AND every page fetch."""
    seen_params: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, json=_ba_response(1))

    httpx_mock.add_callback(method="GET", url=_BA_API, callback=handler, is_reusable=True)
    scraper = BundesagenturScraper("any", angebotsart="34")
    assert scraper.angebotsart == "34"
    jobs = scraper.fetch()
    assert len(jobs) == 1
    assert len(seen_params) >= 2  # size=1 probe + size=100 pages
    for p in seen_params:
        assert p.get("angebotsart") == ["34"]


def test_ba_angebotsart_none_is_default(httpx_mock) -> None:
    httpx_mock.add_response(url=_BA_API, json=_ba_response(1), is_reusable=True)
    default = BundesagenturScraper("any")
    explicit = BundesagenturScraper("any", angebotsart=None)
    assert default.angebotsart is None
    assert explicit.angebotsart is None
    assert default.fetch() and explicit.fetch()


# --- EURES: keywords shape ------------------------------------------------


def test_eures_default_sends_empty_keywords(httpx_mock) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_eures_response(0))

    httpx_mock.add_callback(method="POST", url=_EURES_API, callback=handler, is_reusable=True)
    jobs = EuresScraper("eures").fetch()
    assert jobs == []
    assert all(b.get("keywords") == [] for b in bodies)


def test_eures_keywords_use_object_shape(httpx_mock) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_eures_response(2))

    httpx_mock.add_callback(method="POST", url=_EURES_API, callback=handler, is_reusable=True)
    scraper = EuresScraper("eures", keywords=["Praktikum"])
    assert scraper.keywords == ["Praktikum"]
    jobs = scraper.fetch()
    assert len(jobs) == 2
    for b in bodies:
        assert b["keywords"] == [
            {"keyword": "Praktikum", "specificSearchCode": "EVERYWHERE"}
        ]


def test_eures_multiple_keywords_map_to_entries(httpx_mock) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_eures_response(0))

    httpx_mock.add_callback(method="POST", url=_EURES_API, callback=handler, is_reusable=True)
    EuresScraper("eures", keywords=["Praktikum", "internship"]).fetch()
    for b in bodies:
        assert b["keywords"] == [
            {"keyword": "Praktikum", "specificSearchCode": "EVERYWHERE"},
            {"keyword": "internship", "specificSearchCode": "EVERYWHERE"},
        ]
