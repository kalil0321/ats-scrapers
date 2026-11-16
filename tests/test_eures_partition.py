"""EURES query partitioning: split planning and coverage checks."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx
import pytest

from ats_scrapers.scrapers import eures
from ats_scrapers.scrapers.eures import EuresScraper, _plan_split, _rereads

_API = re.compile(r"^https://europa\.eu/eures/api/jv-searchengine/public/jv-search/search")
_ISCO = "http://data.europa.eu/esco/isco/C"


def _entries(counts: dict[str, int]) -> dict:
    return {"facetEntriesList": [{"code": c, "count": n} for c, n in counts.items()]}


def _locations(tree: dict[str, tuple[int, dict[str, int]]]) -> dict:
    return {
        "facetEntriesList": [
            {
                "code": country,
                "count": count,
                "childrenList": [{"code": r, "count": n} for r, n in regions.items()],
            }
            for country, (count, regions) in tree.items()
        ]
    }


# --- planning ----------------------------------------------------------------


def test_unlocated_slice_splits_by_country_including_unlocated_bucket() -> None:
    facets = {
        "POSITION_LOCATION": _locations({
            "de": (60_000, {"de1": 60_000}),
            "fr": (40_000, {"fr10": 40_000}),
            "NS": (500, {}),
        }),
        "POSITION_OFFERING": _entries({"directhire": 90_000, "NS": 10_500}),
    }

    split = _plan_split(facets, {}, 100_500)

    assert split is not None
    assert split.dimension.name == "location"
    assert [(c.values, c.count) for c in split.children] == [
        (("de",), 60_000), (("fr",), 40_000), (("NS",), 500),
    ]


def test_country_splits_into_its_regions_and_region_does_not_split_again() -> None:
    facets = {
        "POSITION_LOCATION": _locations({
            "de": (30_000, {"de1": 12_000, "de2": 15_000, "de-NS": 3_000}),
            "fr": (9_000, {"fr10": 9_000}),
        }),
    }

    split = _plan_split(facets, {"locationCodes": ["de"]}, 30_000)
    assert split is not None
    assert [c.values[0] for c in split.children] == ["de1", "de2", "de-NS"]

    below = _plan_split(facets, {"locationCodes": ["de1"]}, 12_000)
    assert below is not None
    assert below.dimension.name == "occupation"


def test_prefers_the_split_that_leaves_least_above_the_cap() -> None:
    facets = {
        # 20k left above the cap.
        "POSITION_OFFERING": _entries({"directhire": 30_000, "temporary": 10_000}),
        # 2k left above the cap.
        "REQUIRED_EXPERIENCE": _entries(
            {"NS": 12_000, "none_required": 9_000, "up_to_1_year": 9_000,
             "between_1_and_2_years": 10_000}
        ),
    }

    split = _plan_split(facets, {"locationCodes": ["de1"]}, 40_000)

    assert split is not None
    assert split.dimension.name == "experience"


def test_duplicates_of_a_multi_valued_facet_count_against_it() -> None:
    facets = {
        # Every child fits, but the schedules repeat 25k vacancies.
        "POSITION_SCHEDULE": _entries({"fulltime": 9_000, "parttime": 9_000,
                                        "flextime": 9_000, "NS": 8_000}),
        "POSITION_OFFERING": _entries({"directhire": 6_000, "temporary": 4_001}),
    }

    split = _plan_split(facets, {"locationCodes": ["de1"]}, 10_001)

    assert split is not None
    assert split.dimension.name == "contract"


def test_skips_facets_already_filtered_or_that_do_not_narrow_the_slice() -> None:
    facets = {
        "POSITION_OFFERING": _entries({"directhire": 15_000, "temporary": 15_000}),
        "EURES_FLAG": _entries({"WITHOUT": 30_000}),
        "NACE_CODE": _entries({"o": 30_000, "n": 2_000}),
        "POSITION_SCHEDULE": _entries({"fulltime": 20_000, "parttime": 10_000}),
    }
    base = {"locationCodes": ["de1"], "positionOfferingCodes": ["directhire"]}

    split = _plan_split(facets, base, 30_000)

    assert split is not None
    assert split.dimension.name == "schedule"


def test_prefers_a_facet_whose_counts_reach_the_parent_total() -> None:
    facets = {
        # Sums to 18k of 20k: 2k vacancies would fall outside.
        "POSITION_OFFERING": _entries({"directhire": 9_000, "temporary": 9_000}),
        "POSITION_SCHEDULE": _entries({"fulltime": 14_000, "NS": 6_000}),
    }

    split = _plan_split(facets, {"locationCodes": ["de1"]}, 20_000)

    assert split is not None
    assert split.dimension.name == "schedule"


def test_education_groups_map_to_the_levels_the_filter_accepts() -> None:
    facets = {
        "EDUCATION_AND_QUALIFICATION_LEVEL": _entries(
            {"bachelor": 8_000, "basic": 7_000, "NS": 9_000}
        ),
    }

    split = _plan_split(facets, {"locationCodes": ["be2"]}, 24_000)

    assert split is not None
    assert split.dimension.field == "educationAndQualificationLevelCodes"
    assert [c.values for c in split.children] == [
        ("isced_6", "eqf_6"),
        ("isced_0", "isced_1", "isced_2", "eqf_1", "eqf_2", "eqf_3"),
        ("NS",),
    ]


def test_language_split_adds_one_child_for_languages_the_facet_omits() -> None:
    facets = {
        "LANGUAGE_SKILLS": _entries({"nl": 7_000, "NS": 5_000, "en": 11, "de": 0}),
    }

    split = _plan_split(facets, {"locationCodes": ["nl36"]}, 12_000)

    assert split is not None
    assert split.dimension.field == "requiredLanguages"
    listed, unlisted = split.children[:3], split.children[3]
    assert [c.values for c in listed] == [
        ({"isoCode": "nl", "level": "C2"},),
        ({"isoCode": "NS", "level": "C2"},),
        ({"isoCode": "en", "level": "C2"},),
    ]
    codes = {v["isoCode"] for v in unlisted.values}
    assert {"ru", "lb", "uk"} <= codes
    assert not codes & {"nl", "en", "de", "NS"}
    assert unlisted.count is None


def test_one_listed_language_and_the_unlisted_rest_still_split_the_slice() -> None:
    # 3,000 of the 12,000 require only languages the facet does not list.
    facets = {"LANGUAGE_SKILLS": _entries({"nl": 9_000, "de": 0})}

    split = _plan_split(facets, {"locationCodes": ["nl36"]}, 12_000)

    assert split is not None
    assert split.dimension.field == "requiredLanguages"
    assert len(split.children) == 2


def test_no_unlisted_language_child_when_the_facet_lists_every_code() -> None:
    every = dict.fromkeys(eures._ISO_639_1, 400)
    facets = {"LANGUAGE_SKILLS": _entries({**every, "nl": 9_000, "NS": 3_000})}

    split = _plan_split(facets, {"locationCodes": ["nl36"]}, 12_000)

    assert split is not None
    assert split.dimension.field == "requiredLanguages"
    # An empty child would send no language filter and repeat its parent.
    assert all(child.values for child in split.children)


def test_falls_back_to_the_isco_tree_down_to_unit_groups() -> None:
    split = _plan_split({}, {"locationCodes": ["nl36"]}, 12_000)
    assert split is not None
    assert split.dimension.field == "occupationUris"
    assert [c.values[0] for c in split.children] == [f"{_ISCO}{d}" for d in range(10)]
    assert all(c.count is None for c in split.children)

    deeper = _plan_split({}, {"occupationUris": [f"{_ISCO}25"]}, 12_000)
    assert deeper is not None
    assert [c.values[0] for c in deeper.children][:2] == [f"{_ISCO}250", f"{_ISCO}251"]

    assert _plan_split({}, {"occupationUris": [f"{_ISCO}2512"]}, 12_000) is None


def _rows(*rows: tuple[str, int]) -> list[dict]:
    return [{"id": i, "lastModificationDate": stamp} for i, stamp in rows]


def test_a_short_tied_run_around_a_repeat_is_read_in_one_window() -> None:
    # Positions 47-49 and 50-51 share a timestamp; t3 came back on both pages.
    page_1 = _rows(*((f"p{i}", 100 - i) for i in range(47)),
                   ("t1", 5), ("t2", 5), ("t3", 5))
    page_2 = _rows(("t3", 5), ("t4", 5),
                   *((f"q{i}", 4 - i) for i in range(48)))

    # Page 6 of 9 results holds positions 45-53.
    assert _rereads({1: page_1, 2: page_2}) == [(6, 9)]


def test_a_tied_run_longer_than_a_page_gets_its_pages_read_again() -> None:
    page_1 = _rows(*((f"p{i}", 100 - i) for i in range(20)),
                   *((f"t{i}", 5) for i in range(30)))
    page_2 = _rows(("t29", 5), *((f"u{i}", 5) for i in range(30)),
                   *((f"q{i}", 4 - i) for i in range(19)))
    page_3 = _rows(*((f"r{i}", 1) for i in range(50)))

    assert _rereads({1: page_1, 2: page_2, 3: page_3}) == [(1, None), (2, None)]


def test_nothing_is_read_again_without_a_repeat() -> None:
    page_1 = _rows(*((f"p{i}", 5) for i in range(50)))
    page_2 = _rows(*((f"q{i}", 5) for i in range(10)))

    assert _rereads({1: page_1, 2: page_2}) == []


# --- end to end against a fake portal ------------------------------------------


@dataclass
class _Vacancy:
    id: str
    country: str
    region: str
    contract: str = "directhire"
    schedules: tuple[str, ...] = ("fulltime",)
    occupations: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("NS",)
    modified: int = 0


_FILTERS = {
    "locationCodes": lambda v, codes: v.country in codes or v.region in codes,
    "positionOfferingCodes": lambda v, codes: v.contract in codes,
    "positionScheduleCodes": lambda v, codes: bool(set(v.schedules) & set(codes)),
    "requiredLanguages": lambda v, langs: bool(
        set(v.languages) & {lang["isoCode"] for lang in langs}
    ),
    "occupationUris": lambda v, uris: any(
        o.startswith(u.removeprefix(_ISCO)) for o in v.occupations for u in uris
    ),
}
# Like the portal, the language facet lists a fixed set of languages.
_FACET_LANGUAGES = ("de", "en", "nl", "NS")


@dataclass
class _FakePortal:
    """In-memory search API with the portal's semantics: values of one
    filter are ORed, filters are ANDed, each facet ignores its own filter,
    and a page ending past ``max_results`` is rejected. With
    ``shuffle_ties``, vacancies sharing ``modified`` swap order on every
    other read of a page number."""

    vacancies: list[_Vacancy]
    max_results: int
    rejected: set[str] = field(default_factory=set)
    shuffle_ties: bool = False
    bodies: list[dict] = field(default_factory=list)
    reads: dict[int, int] = field(default_factory=dict)

    def _matching(self, body: dict, skip: str | None = None) -> list[_Vacancy]:
        out = []
        for v in self.vacancies:
            if all(
                not body.get(key) or match(v, body[key])
                for key, match in _FILTERS.items()
                if key != skip
            ):
                out.append(v)
        return out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.bodies.append(body)
        if body["page"] * body["resultsPerPage"] > self.max_results or any(
            value in self.rejected for values in body.values()
            if isinstance(values, list) for value in values if isinstance(value, str)
        ):
            return httpx.Response(400, json={"error": "bad request"})
        hits = sorted(self._matching(body), key=lambda v: -v.modified)
        number = body["page"]
        self.reads[number] = self.reads.get(number, 0) + 1
        if self.shuffle_ties and (number + self.reads[number]) % 2:
            position = {v.id: i for i, v in enumerate(hits)}
            hits.sort(key=lambda v: (-v.modified, -position[v.id]))
        size = body["resultsPerPage"]
        page = hits[(number - 1) * size: number * size]

        def counts(key: str, values, listed=None) -> dict:
            out: dict[str, int] = dict.fromkeys(listed or (), 0)
            for v in self._matching(body, skip=key):
                for value in values(v):
                    if listed is None or value in listed:
                        out[value] = out.get(value, 0) + 1
            return _entries(out)

        located = self._matching(body, skip="locationCodes")
        tree: dict[str, tuple[int, dict[str, int]]] = {}
        for v in located:
            count, regions = tree.get(v.country, (0, {}))
            regions[v.region] = regions.get(v.region, 0) + 1
            tree[v.country] = (count + 1, regions)
        return httpx.Response(200, json={
            "numberRecords": len(hits),
            "jvs": [
                {
                    "id": v.id,
                    "title": f"Job {v.id}",
                    "locationMap": {},
                    "lastModificationDate": v.modified,
                }
                for v in page
            ],
            "facets": {
                "POSITION_LOCATION": _locations(tree),
                "POSITION_OFFERING": counts(
                    "positionOfferingCodes", lambda v: [v.contract],
                ),
                "POSITION_SCHEDULE": counts(
                    "positionScheduleCodes", lambda v: v.schedules,
                ),
                "LANGUAGE_SKILLS": counts(
                    "requiredLanguages", lambda v: v.languages, _FACET_LANGUAGES,
                ),
            },
        })


@pytest.fixture
def small_pages(monkeypatch):
    """Cap every query at 4 results (2 per page, 2 pages)."""
    monkeypatch.setattr(eures, "PAGE_SIZE", 2)
    monkeypatch.setattr(eures, "PAGE_LIMIT", 2)
    monkeypatch.setattr(eures, "PAGINATION_CAP", 4)


def _serve(httpx_mock, portal: _FakePortal) -> None:
    httpx_mock.add_callback(portal, method="POST", url=_API, is_reusable=True)


def _ids(jobs) -> set[str]:
    return {j.ats_id for j in jobs}


def test_collects_every_vacancy_through_nested_splits(
    httpx_mock, small_pages, caplog,
) -> None:
    vacancies = [
        _Vacancy("ns1", "NS", "NS"),
        *(_Vacancy(f"fr{i}", "fr", "fr10") for i in range(3)),
        # 14 vacancies in one region: contract, then occupation narrow
        # them below the cap.
        *(
            _Vacancy(
                f"de{i}", "de", "de1",
                contract=("directhire", "temporary")[i % 2],
                schedules=(("fulltime",), ("parttime", "fulltime"))[i % 3 == 0],
                occupations=(f"{2 + i % 4}{i % 3}11",),
            )
            for i in range(14)
        ),
        *(_Vacancy(f"dx{i}", "de", "de-NS") for i in range(2)),
    ]
    portal = _FakePortal(vacancies, max_results=4)
    _serve(httpx_mock, portal)

    with caplog.at_level(logging.WARNING):
        jobs = EuresScraper("eures").fetch()

    assert _ids(jobs) == {v.id for v in vacancies}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(b["occupationUris"] for b in portal.bodies)
    # Each split was checked with one query ORing all of its children.
    assert any(len(b["locationCodes"]) > 1 for b in portal.bodies)


def test_tied_vacancies_skipped_between_pages_are_read_in_one_window(
    httpx_mock, monkeypatch,
) -> None:
    monkeypatch.setattr(eures, "PAGE_SIZE", 3)
    monkeypatch.setattr(eures, "PAGE_LIMIT", 2)
    monkeypatch.setattr(eures, "PAGINATION_CAP", 6)
    # c and d share a timestamp across the page 1|2 boundary.
    vacancies = [
        _Vacancy(v, "mt", "mt001", modified=stamp)
        for v, stamp in (("a", 9), ("b", 8), ("c", 5), ("d", 5), ("e", 2), ("f", 1))
    ]
    portal = _FakePortal(vacancies, max_results=6, shuffle_ties=True)
    _serve(httpx_mock, portal)

    jobs = EuresScraper("eures").fetch()

    assert _ids(jobs) == {"a", "b", "c", "d", "e", "f"}
    assert any(b["resultsPerPage"] == 2 for b in portal.bodies)


def test_language_split_reaches_vacancies_in_languages_the_facet_omits(
    httpx_mock, small_pages, caplog,
) -> None:
    vacancies = [
        _Vacancy("f0", "fr", "fr10"),
        *(_Vacancy(f"n{i}", "nl", "nl36", languages=("nl",)) for i in range(4)),
        *(_Vacancy(f"x{i}", "nl", "nl36") for i in range(3)),
        _Vacancy("ru", "nl", "nl36", languages=("ru",)),
        _Vacancy("lb", "nl", "nl36", languages=("lb", "de")),
    ]
    portal = _FakePortal(vacancies, max_results=4)
    _serve(httpx_mock, portal)

    with caplog.at_level(logging.WARNING):
        jobs = EuresScraper("eures").fetch()

    assert _ids(jobs) == {v.id for v in vacancies}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any(b["occupationUris"] for b in portal.bodies)


def test_vacancies_outside_every_occupation_are_reported_and_read_from_the_slice(
    httpx_mock, small_pages, caplog,
) -> None:
    unclassified = [_Vacancy(f"c{i}", "nl", "nl36") for i in range(2)]
    vacancies = [
        _Vacancy("f0", "fr", "fr10"),
        _Vacancy("n0", "nl", "nl11"),
        *(_Vacancy(f"a{i}", "nl", "nl36", occupations=("2512",)) for i in range(2)),
        # Second page of the nl36 slice, so only the slice itself returns them.
        *unclassified,
        _Vacancy("a2", "nl", "nl36", occupations=("2512",)),
        *(_Vacancy(f"b{i}", "nl", "nl36", occupations=("5223",)) for i in range(3)),
    ]
    _serve(httpx_mock, _FakePortal(vacancies, max_results=4))

    with caplog.at_level(logging.WARNING):
        jobs = EuresScraper("eures").fetch()

    assert _ids(jobs) == {v.id for v in vacancies}
    assert any(
        "split of locationCodes=nl36 by occupation covers 6 of 8" in r.getMessage()
        and "2 fall outside it" in r.getMessage()
        for r in caplog.records
    )


def test_warns_when_a_slice_cannot_be_split_below_the_cap(
    httpx_mock, small_pages, caplog,
) -> None:
    vacancies = [_Vacancy(f"x{i}", "mt", "mt001", occupations=("9112",)) for i in range(6)]
    _serve(httpx_mock, _FakePortal(vacancies, max_results=4))

    with caplog.at_level(logging.WARNING):
        jobs = EuresScraper("eures").fetch()

    assert len(jobs) == 4
    assert any(
        "holds 6 vacancies and cannot be split further; 2 beyond the first 4"
        in r.getMessage()
        for r in caplog.records
    )


def test_rejected_filter_value_is_reported_instead_of_read_as_empty(
    httpx_mock, small_pages, caplog,
) -> None:
    vacancies = [
        _Vacancy("f0", "fr", "fr10"),
        *(_Vacancy(f"d{i}", "de", "de1") for i in range(3)),
        *(_Vacancy(f"e{i}", "de", "de2") for i in range(3)),
    ]
    _serve(httpx_mock, _FakePortal(vacancies, max_results=4, rejected={"de2"}))

    with caplog.at_level(logging.WARNING):
        jobs = EuresScraper("eures").fetch()

    assert _ids(jobs) >= {f"d{i}" for i in range(3)}
    assert any(
        "location subtask failed" in r.getMessage()
        and "rejected the query locationCodes=de2" in r.getMessage()
        for r in caplog.records
    )
