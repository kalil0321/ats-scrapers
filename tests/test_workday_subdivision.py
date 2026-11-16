"""Workday boards above the 2,000-job search cap are split by facet."""

from __future__ import annotations

import json

import httpx

from ats_scrapers.scrapers.workday import (
    QUERY_TOTAL_CAP,
    WorkdayScraper,
    _pick_subdivision_facet,
)

URL = "https://acme.wd1.myworkdayjobs.com/External"
API = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"

DISTANCE = {
    "facetParameter": "distance",
    "values": [
        {"descriptor": "10 miles", "id": "d10", "count": 1},
        {"descriptor": "Any Distance", "id": "dany", "count": 1},
    ],
}


def _values(counts: dict[str, int]) -> list[dict]:
    return [{"descriptor": key, "id": key, "count": count} for key, count in counts.items()]


def _location_group(countries: dict[str, int], cities: dict[str, int]) -> dict:
    return {
        "facetParameter": "locationMainGroup",
        "values": [
            {"facetParameter": "locationCountry", "values": _values(countries)},
            {"facetParameter": "locations", "values": _values(cities)},
        ],
    }


def test_nested_location_facets_are_used() -> None:
    facets = [
        {"facetParameter": "jobFamilyGroup", "values": _values({"eng": 3000, "ops": 900})},
        {"facetParameter": "timeType", "values": _values({"ft": 3900})},
        _location_group({"in": 2500, "us": 1400}, {"blr": 1500, "pun": 1000}),
        DISTANCE,
    ]

    picked = _pick_subdivision_facet(facets, already_applied={"jobFamilyGroup"})

    assert picked == ("locationCountry", [("in", 2500), ("us", 1400)])


def test_distance_and_single_value_facets_do_not_split() -> None:
    facets = [
        {"facetParameter": "timeType", "values": _values({"ft": 3900, "pt": 0})},
        DISTANCE,
    ]

    assert _pick_subdivision_facet(facets, already_applied=set()) is None


def test_facet_that_misses_jobs_is_a_last_resort() -> None:
    # 3,000 jobs: 1,200 have no country, every one has a city.
    facets = [_location_group({"in": 1000, "us": 800}, {"blr": 1900, "nyc": 1300})]

    assert _pick_subdivision_facet(facets, already_applied=set(), size=3000)[0] == "locations"
    assert _pick_subdivision_facet(
        facets, already_applied={"locations"}, size=3000,
    )[0] == "locationCountry"


def _posting(key: str) -> dict:
    return {"title": f"Job {key}", "externalPath": f"/job/{key}", "bulletFields": [key]}


def test_capped_board_is_split_by_country_then_city(httpx_mock) -> None:
    """Only the nested location facets split this board below the cap."""
    jobs_by_city = {
        ("in", "blr"): [f"R{i}" for i in range(15)],
        ("in", "pun"): [f"R{i}" for i in range(15, 27)],
        ("us", None): [f"R{i}" for i in range(27, 35)],
    }
    facets = [
        {"facetParameter": "timeType", "values": _values({"ft": 35})},
        _location_group({"in": 27, "us": 8}, {"blr": 15, "pun": 12, "nyc": 8}),
        DISTANCE,
    ]

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        applied = body["appliedFacets"]
        assert "distance" not in applied
        country = (applied.get("locationCountry") or [None])[0]
        city = (applied.get("locations") or [None])[0]
        if country == "us":
            keys = jobs_by_city[("us", None)]
            total = len(keys)
        elif country == "in" and city:
            keys = jobs_by_city.get(("in", city), [])
            total = len(keys)
        else:
            # The board and India alone are both above the cap.
            keys = jobs_by_city[("in", "blr")]
            total = QUERY_TOTAL_CAP
        offset = body["offset"]
        page = keys[offset:offset + 20] if total != QUERY_TOTAL_CAP else keys[:20]
        return httpx.Response(
            200,
            json={"total": total, "jobPostings": [_posting(k) for k in page], "facets": facets},
        )

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    jobs = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in jobs) == sorted(
        key for keys in jobs_by_city.values() for key in keys
    )


def test_capped_search_with_nothing_left_to_split_is_logged(httpx_mock, caplog) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        return httpx.Response(
            200,
            json={
                "total": QUERY_TOTAL_CAP,
                "jobPostings": [_posting(f"R{offset + i}") for i in range(20)],
                "facets": [DISTANCE],
            },
        )

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    with caplog.at_level("WARNING"):
        jobs = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert len(jobs) == QUERY_TOTAL_CAP
    assert "cannot be split further" in caplog.text
