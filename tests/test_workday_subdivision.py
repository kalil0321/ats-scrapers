"""Workday boards above the 2,000-job search cap are split by facet."""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest

from ats_scrapers.scrapers import workday
from ats_scrapers.scrapers.workday import (
    FACET_VALUE_CAP,
    QUERY_TOTAL_CAP,
    WorkdayScraper,
    _pick_subdivision_facet,
    _remainder_plan,
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


def test_a_facet_the_search_filters_on_does_not_size_it() -> None:
    """A filtered facet counts the jobs of the search without its filter.

    A state holds 2,013 jobs; its time types, already filtered on, count
    2,075. Worker type covers the 2,013; the cities listed reach 467.
    """
    facets = [
        {"facetParameter": "timeType", "values": _values({"ft": 2013, "pt": 62})},
        {"facetParameter": "workerSubType", "values": _values({"regular": 1500, "seasonal": 513})},
        {"facetParameter": "locations", "values": _values({f"c{i}": 8 for i in range(54)} | {"c54": 35})},
    ]

    picked = _pick_subdivision_facet(
        facets, already_applied={"jobFamilyGroup", "timeType", "state"}, size=2013,
    )

    assert picked[0] == "workerSubType"


def test_a_facet_with_several_values_per_job_does_not_size_the_board() -> None:
    """Skills tag each job several times and list only the top values."""
    skills = {f"s{i}": 1200 - i for i in range(FACET_VALUE_CAP)}
    facets = [
        {"facetParameter": "jobFamilyGroup", "values": _values({"eng": 19000, "ops": 20455})},
        {"facetParameter": "timeType", "values": _values({"ft": 39428, "pt": 28})},
        {"facetParameter": "workerSubType", "values": _values(skills)},
    ]

    picked = _pick_subdivision_facet(facets, already_applied=set(), size=QUERY_TOTAL_CAP)

    assert picked[0] == "jobFamilyGroup"


def test_a_facet_with_one_value_per_job_comes_before_one_counting_jobs_twice() -> None:
    """Each job names two cities: splitting on them reads it twice."""
    facets = [
        {"facetParameter": "locations", "values": _values({"nyc": 3000, "sf": 2000, "la": 1000})},
        {"facetParameter": "jobFamily", "values": _values({"eng": 2400, "ops": 600})},
    ]

    picked = _pick_subdivision_facet(facets, already_applied={"jobFamilyGroup"}, size=3000)

    assert picked[0] == "jobFamily"


def test_remainder_plan_finds_the_values_holding_the_missed_jobs() -> None:
    node = [
        {"facetParameter": "timeType", "values": _values({"ft": 1500, "pt": 600})},
        {"facetParameter": "locationCountry", "values": _values({"us": 2000, "ca": 900, "mx": 400})},
        {"facetParameter": "workerSubType", "values": _values({"regular": 3000, "seasonal": 300})},
    ]
    reached = [
        {"facetParameter": "timeType", "values": _values({"ft": 1500, "pt": 600})},
        {"facetParameter": "locationCountry", "values": _values({"us": 1999, "ca": 300, "mx": 400})},
        {"facetParameter": "workerSubType", "values": _values({"regular": 2000, "seasonal": 100})},
    ]

    assert _remainder_plan(node, [(1, reached)], skip={"timeType"}) == (
        "workerSubType", [("regular", 3000, 1000), ("seasonal", 300, 200)],
    )
    assert _remainder_plan(node, [(1, reached)], skip={"timeType", "workerSubType"}) == (
        "locationCountry", [("us", 2000, 1), ("ca", 900, 600)],
    )
    assert _remainder_plan(node, [(1, node)], skip=set()) is None


def test_remainder_plan_bounds_values_a_full_listing_leaves_out() -> None:
    """The reached listing stops at 55 cities; the smallest it shows holds 46."""
    cities = {f"c{i}": 100 - i for i in range(FACET_VALUE_CAP)}
    node = [{"facetParameter": "locations", "values": _values({**cities, "rare": 40, "big": 60})}]
    reached = [{"facetParameter": "locations", "values": _values({**cities, "c0": 90})}]

    assert _remainder_plan(node, [(1, reached)], skip=set()) == (
        "locations", [("c0", 100, 10), ("big", 60, 14)],
    )


def test_board_root_size_is_the_largest_single_value_facet() -> None:
    """The root reports 2,000; time type shows the board holds 30,000."""
    facets = [
        {"facetParameter": "jobFamilyGroup", "values": _values({"a": 15000, "b": 10000})},
        {"facetParameter": "timeType", "values": _values({"ft": 28000, "pt": 2000})},
    ]

    picked = _pick_subdivision_facet(facets, already_applied=set(), size=QUERY_TOTAL_CAP)

    assert picked[0] == "timeType"


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


def test_board_root_split_skips_a_facet_that_misses_jobs(httpx_mock) -> None:
    """The board reports the 2,000 cap; its countries cover only 1,500 jobs."""
    facets = [_location_group({"in": 900, "us": 600}, {"blr": 1200, "nyc": 900, "pun": 700})]
    applied: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        applied.append(body["appliedFacets"])
        if not body["appliedFacets"]:
            return httpx.Response(200, json={
                "total": QUERY_TOTAL_CAP, "jobPostings": [], "facets": facets,
            })
        (values,) = body["appliedFacets"].values()
        if len(values) > 1:
            # The check for jobs without a city: every job has one.
            return httpx.Response(200, json={
                "total": QUERY_TOTAL_CAP, "jobPostings": [], "facets": facets,
            })
        return httpx.Response(200, json={
            "total": 1, "jobPostings": [_posting(values[0])], "facets": [],
        })

    httpx_mock.add_callback(reply, url=API, is_reusable=True)

    WorkdayScraper(URL, include_descriptions=False).fetch()

    assert {param for filters in applied for param in filters} == {"locations"}


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


class _Board:
    """A Workday search over ``jobs``, each a map of facet to its values.

    Facets combine with AND, the values of one facet with OR. Each facet
    counts the jobs the other filters keep, lists at most ``value_cap``
    values, and the total stops at the cap.
    """

    def __init__(
        self, jobs: dict[str, dict[str, list[str]]], *, value_cap: int = FACET_VALUE_CAP,
    ) -> None:
        self.jobs = jobs
        self.value_cap = value_cap
        self.params = sorted({param for job in jobs.values() for param in job})
        self.searches: list[dict[str, list[str]]] = []
        self.pages: list[tuple[str, int]] = []

    def _matching(self, applied: dict[str, list[str]], *, but: str | None = None) -> list[str]:
        return [
            key for key, job in self.jobs.items()
            if all(
                set(job.get(param, ())) & set(values)
                for param, values in applied.items() if param != but
            )
        ]

    def reply(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        applied = body["appliedFacets"]
        if body["offset"] == 0:
            self.searches.append(applied)
        self.pages.append((json.dumps(applied, sort_keys=True), body["offset"]))
        hits = self._matching(applied)
        facets = []
        for param in self.params:
            counts = Counter(
                value for key in self._matching(applied, but=param)
                for value in self.jobs[key].get(param, ())
            )
            facets.append({
                "facetParameter": param,
                "values": _values(dict(counts.most_common(self.value_cap))),
            })
        cap = workday.QUERY_TOTAL_CAP
        offset = body["offset"] if body["offset"] < cap else 0
        return httpx.Response(200, json={
            "total": min(len(hits), cap),
            "jobPostings": [_posting(key) for key in hits[offset:offset + 20]],
            "facets": facets,
        })


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(workday, "QUERY_TOTAL_CAP", 40)
    return 40


def test_jobs_the_split_facet_misses_are_read_through_another(httpx_mock, small_cap) -> None:
    """30 of 100 jobs have no time type; their countries reach them."""
    jobs = {
        f"R{i}": {
            "locationCountry": [f"c{i % 5}"],
            **({"timeType": ["ft" if i < 35 else "pt"]} if i < 70 else {}),
        }
        for i in range(100)
    }
    board = _Board(jobs)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)
    assert {"timeType": ["ft", "pt"]} in board.searches


def test_jobs_no_facet_reaches_are_logged(httpx_mock, small_cap, caplog) -> None:
    jobs = {
        f"R{i}": {
            "jobFamilyGroup": ["a" if i < 90 else "b"],
            **({"timeType": ["ft" if i < 30 else "pt"]} if i < 60 else {}),
        }
        for i in range(100)
    }
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.workday"):
        WorkdayScraper(URL, include_descriptions=False).fetch()

    assert "about 30 of 90 jobs of a search filtered on ['jobFamilyGroup'] have no timeType" in caplog.text


def test_the_check_for_missed_jobs_stays_within_the_values_one_search_takes(
    httpx_mock, small_cap,
) -> None:
    """Workday answers HTTP 500 to a search filtered on hundreds of values.

    A board's own search lists all its cities."""
    jobs = {f"R{i}": {"locations": [f"city{i // 2}"]} for i in range(300)}
    board = _Board(jobs, value_cap=1000)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert len(scraped) == 300
    assert max(len(values) for search in board.searches for values in search.values()) == 1


def test_a_large_value_holding_few_missed_jobs_is_narrowed_down(httpx_mock, small_cap) -> None:
    """Two of 150 jobs in one country have no time type: a city holds them."""
    jobs = {
        f"R{i}": {
            "locationCountry": ["c0" if i < 150 else "c1"],
            "locations": [f"city{i // 15}"],
            **({"timeType": ["ft" if i % 2 else "pt"]} if i not in (3, 4) else {}),
        }
        for i in range(200)
    }
    board = _Board(jobs)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)
    # Only the city holding them is read again, not the whole country.
    assert {"locationCountry": ["c0"], "locations": ["city0"]} in board.searches
    assert {"locationCountry": ["c0"], "locations": ["city1"]} not in board.searches


def test_jobs_without_a_skill_are_logged(httpx_mock, small_cap, caplog) -> None:
    """Skills tag 30 of 55 jobs twice each; the other 25 have none."""
    jobs = {
        f"R{i}": {
            "jobFamilyGroup": ["a"],
            **({"workerSubType": [f"s{i % 3}", f"s{i % 3 + 3}"]} if i < 30 else {}),
        }
        for i in range(55)
    }
    jobs["R99"] = {"jobFamilyGroup": ["b"]}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.workday"):
        WorkdayScraper(URL, include_descriptions=False).fetch()

    assert "about 25 of 55 jobs of a search filtered on ['jobFamilyGroup'] have no workerSubType" in caplog.text


def test_a_value_no_facet_narrows_is_read_whole(httpx_mock, monkeypatch) -> None:
    """One of 150 jobs in a country has no time type, and nothing else tells it apart."""
    monkeypatch.setattr(workday, "QUERY_TOTAL_CAP", 200)
    jobs = {
        f"R{i}": {
            "locationCountry": ["c0" if i < 150 else "c1"],
            **({"timeType": ["ft" if i % 2 else "pt"]} if i != 141 else {}),
        }
        for i in range(250)
    }
    board = _Board(jobs)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)


def test_jobs_the_first_recovery_facet_misses_are_read_through_a_second(httpx_mock, small_cap) -> None:
    """Of the jobs with no time type, 15 have only a country and 10 only a worker type."""
    jobs: dict[str, dict[str, list[str]]] = {
        f"R{i}": {"timeType": ["ft" if i % 2 else "pt"]} for i in range(60)
    }
    jobs |= {f"C{i}": {"locationCountry": [f"c{i % 2}"]} for i in range(15)}
    jobs |= {f"W{i}": {"workerSubType": [f"w{i % 2}"]} for i in range(10)}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)


def test_a_capped_value_no_facet_narrows_is_read_as_far_as_it_shows(httpx_mock, small_cap) -> None:
    """One of 120 jobs in a country has no time type; the country's search shows it."""
    jobs = {
        f"R{i}": {
            "locationCountry": ["c0" if i < 120 else "c1"],
            **({"timeType": ["ft" if i % 2 else "pt"]} if i != 30 else {}),
        }
        for i in range(150)
    }
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert "R30" in {job.requisition_id for job in scraped}


def test_jobs_without_a_skill_that_cannot_be_counted_are_logged(httpx_mock, small_cap, caplog) -> None:
    """60 of 90 jobs have two of 60 skills, listed 55 at most; the search
    for them stops at the cap."""
    jobs = {
        f"R{i}": {
            "jobFamilyGroup": ["a"],
            **({"workerSubType": [f"s{i}", f"s{(i + 1) % 60}"]} if i < 60 else {}),
        }
        for i in range(90)
    }
    jobs["R99"] = {"jobFamilyGroup": ["b"]}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    with caplog.at_level("WARNING", logger="ats_scrapers.scrapers.workday"):
        WorkdayScraper(URL, include_descriptions=False).fetch()

    assert "workerSubType gives some several values and lists only 55" in caplog.text


def test_a_full_listing_with_several_values_per_job_is_not_paged(httpx_mock, small_cap) -> None:
    """Each job names two of six cities: the cities are taken to reach them all."""
    jobs = {
        f"R{i}": {"jobFamilyGroup": ["a"], "locations": [f"c{i % 3}", f"c{i % 3 + 3}"]}
        for i in range(90)
    }
    jobs["R99"] = {"jobFamilyGroup": ["b"]}
    board = _Board(jobs)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    WorkdayScraper(URL, include_descriptions=False).fetch()

    assert (json.dumps({"jobFamilyGroup": ["a"]}), 20) not in board.pages


def test_jobs_only_a_later_facet_reaches_are_read(httpx_mock, small_cap) -> None:
    """70 jobs have every facet; of the rest, 20 have only a city, 10 only a
    country and 6 only a worker type."""
    jobs: dict[str, dict[str, list[str]]] = {
        f"R{i}": {
            "timeType": ["ft" if i % 2 else "pt"],
            "locationCountry": [f"c{i % 3}"],
            "locations": [f"city{i % 4}"],
            "workerSubType": [f"w{i % 3}"],
        }
        for i in range(70)
    }
    jobs |= {f"T{i}": {"locations": [f"city{i % 4}"]} for i in range(20)}
    jobs |= {f"C{i}": {"locationCountry": [f"c{i % 3}"]} for i in range(10)}
    jobs |= {f"W{i}": {"workerSubType": [f"w{i % 3}"]} for i in range(6)}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)


def test_a_value_a_full_listing_leaves_out_is_read(httpx_mock, small_cap) -> None:
    """Typed jobs fill the 55 countries a listing shows; the untyped ones are in another."""
    jobs: dict[str, dict[str, list[str]]] = {
        f"R{i}": {"timeType": ["ft" if i % 2 else "pt"], "locationCountry": [f"c{i % 55}"]}
        for i in range(110)
    }
    jobs |= {f"U{i}": {"locationCountry": ["other"]} for i in range(5)}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)


def test_a_value_a_later_facet_cannot_narrow_is_read_as_far_as_it_shows(httpx_mock, monkeypatch) -> None:
    """After the country-only jobs, one job with only a worker type is left in a 150-job value."""
    monkeypatch.setattr(workday, "QUERY_TOTAL_CAP", 200)
    jobs: dict[str, dict[str, list[str]]] = {
        f"R{i}": {
            "timeType": ["ft" if i % 2 else "pt"],
            **({"locationCountry": ["c0"]} if i < 20 else {}),
            **({"workerSubType": ["w0"]} if i < 149 else {}),
        }
        for i in range(300)
    }
    jobs |= {f"C{i}": {"locationCountry": ["c0"]} for i in range(10)}
    jobs["W0"] = {"workerSubType": ["w0"]}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = WorkdayScraper(URL, include_descriptions=False).fetch()

    assert sorted(job.requisition_id for job in scraped) == sorted(jobs)


def test_a_capped_search_shows_jobs_no_facet_reaches(httpx_mock, small_cap) -> None:
    """60 of 90 jobs have two of 60 skills, listed 55 at most; the search's
    own pages show the others."""
    jobs = {
        f"R{i}": {
            "jobFamilyGroup": ["a"],
            **({"workerSubType": [f"s{i}", f"s{(i + 1) % 90}"]} if i >= 30 else {}),
        }
        for i in range(90)
    }
    jobs["R99"] = {"jobFamilyGroup": ["b"]}
    httpx_mock.add_callback(_Board(jobs).reply, url=API, is_reusable=True)

    scraped = {job.requisition_id for job in WorkdayScraper(URL, include_descriptions=False).fetch()}

    assert {f"R{i}" for i in range(30)} <= scraped


def test_a_large_search_is_not_paged_for_one_missed_job(httpx_mock, small_cap) -> None:
    jobs = {
        f"R{i}": {
            "jobFamilyGroup": ["a"],
            **({"timeType": ["ft" if i % 2 else "pt"]} if i else {}),
        }
        for i in range(100)
    }
    jobs["R999"] = {"jobFamilyGroup": ["b"]}
    board = _Board(jobs)
    httpx_mock.add_callback(board.reply, url=API, is_reusable=True)

    WorkdayScraper(URL, include_descriptions=False).fetch()

    assert (json.dumps({"jobFamilyGroup": ["a"]}), 20) not in board.pages
