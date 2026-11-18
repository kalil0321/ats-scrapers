"""Arbetsförmedlingen: walking past the 2,100-ad window of one query."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import WorkloadPercentages
from ats_scrapers.scrapers import ArbetsformedlingenScraper

_API_RE = re.compile(r"^https://jobsearch\.api\.jobtechdev\.se/search")


@pytest.mark.parametrize(("scope", "expected"), [
    ({"min": 0, "max": 100}, WorkloadPercentages(minimum=0, maximum=100)),
    ({"min": 100, "max": 100}, WorkloadPercentages(minimum=100, maximum=100)),
    ({"min": 62.5, "max": None}, WorkloadPercentages(minimum=62.5)),
    ({"max": 80}, WorkloadPercentages(maximum=80)),
    ({"min": None, "max": None}, None),
    ({"min": 90, "max": 10}, None),
    ({"min": False, "max": 80}, None),
    ({"max": 101}, None),
    (None, None),
])
def test_scope_of_work_uses_only_source_bounds(scope, expected):
    row = _ads([datetime(2026, 11, 18)])[0]
    row.update(scope_of_work=scope, working_hours_type={"label": "Deltid"})
    job = ArbetsformedlingenScraper("any")._parse(row)
    assert job.workload_percentages == expected
    assert job.commitment == "Deltid"
    if scope is not None:
        assert job.raw["scope_of_work"] == scope


def _ads(dates: list[datetime]) -> list[dict[str, object]]:
    return [
        {
            "id": str(i),
            "headline": f"Job {i}",
            "webpage_url": f"https://arbetsformedlingen.se/platsbanken/annonser/{i}",
            "publication_date": date.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        for i, date in enumerate(dates)
    ]


def _fake_api(ads: list[dict[str, object]]):
    """Serve ``ads`` with the API's filters, sort orders and offset limit."""

    def respond(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 10))
        if offset > 2000 or limit > 100:
            return httpx.Response(400, json={"cause": {"code": "400"}})
        after = params.get("published-after", "")
        before = params.get("published-before", "9999")
        matches = [ad for ad in ads if after <= str(ad["publication_date"]) <= before]
        matches.sort(
            key=lambda ad: str(ad["publication_date"]),
            reverse=params.get("sort") != "pubdate-asc",
        )
        return httpx.Response(200, json={
            "total": {"value": len(matches)},
            "hits": matches[offset:offset + limit],
        })

    return respond


def test_splits_publication_dates_until_every_window_is_readable(httpx_mock) -> None:
    start = datetime(2026, 1, 1)
    ads = _ads([start + timedelta(minutes=i) for i in range(5_000)])
    httpx_mock.add_callback(_fake_api(ads), url=_API_RE, is_reusable=True)

    jobs = ArbetsformedlingenScraper("arbetsformedlingen").fetch()

    assert len(jobs) == 5_000
    offsets = [
        int(parse_qs(urlsplit(str(r.url)).query).get("offset", ["0"])[0])
        for r in httpx_mock.get_requests()
    ]
    assert max(offsets) <= 2000


def test_warns_when_one_second_holds_more_than_a_window(httpx_mock, caplog) -> None:
    ads = _ads([datetime(2026, 1, 1)] * 2_200)
    httpx_mock.add_callback(_fake_api(ads), url=_API_RE, is_reusable=True)

    with caplog.at_level(logging.WARNING):
        jobs = ArbetsformedlingenScraper("arbetsformedlingen").fetch()

    assert len(jobs) == 2_100
    assert "only 2100 of them are reachable" in caplog.text


def test_newest_ad_stamped_with_a_fraction_of_a_second_is_read(httpx_mock) -> None:
    ads = _ads([datetime(2026, 1, 1, 11), datetime(2026, 1, 1, 11, 30)])
    newest = _ads([datetime(2026, 1, 1, 12)])[0]
    ads.append({**newest, "id": "late", "publication_date": "2026-01-01T12:00:00.500"})
    httpx_mock.add_callback(_fake_api(ads), url=_API_RE, is_reusable=True)

    jobs = ArbetsformedlingenScraper("arbetsformedlingen").fetch()

    assert {j.ats_id for j in jobs} == {"0", "1", "late"}


def test_search_that_ignores_the_date_filter_raises(httpx_mock) -> None:
    ads = _ads([datetime(2026, 1, 1) + timedelta(minutes=i) for i in range(5_000)])
    serve = _fake_api(ads)

    def ignore_dates(request: httpx.Request) -> httpx.Response:
        url = httpx.URL(str(request.url)).copy_remove_param("published-after")
        return serve(httpx.Request("GET", url.copy_remove_param("published-before")))

    httpx_mock.add_callback(ignore_dates, url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="no longer filters by publication date"):
        ArbetsformedlingenScraper("arbetsformedlingen").fetch()

    assert len(httpx_mock.get_requests()) < 10


def test_date_walk_stops_at_its_window_ceiling(httpx_mock, monkeypatch) -> None:
    from ats_scrapers.scrapers import arbetsformedlingen

    monkeypatch.setattr(arbetsformedlingen, "MAX_DATE_WINDOWS", 3)
    ads = _ads([datetime(2026, 1, 1) + timedelta(minutes=i) for i in range(5_000)])
    httpx_mock.add_callback(_fake_api(ads), url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="passed 3 windows"):
        ArbetsformedlingenScraper("arbetsformedlingen").fetch()


def test_empty_board(httpx_mock) -> None:
    httpx_mock.add_callback(_fake_api([]), url=_API_RE, is_reusable=True)

    assert ArbetsformedlingenScraper("arbetsformedlingen").fetch() == []


def test_jobtech_primary_occupation_and_weighted_education() -> None:
    row = _ads([datetime(2026, 11, 18)])[0]
    row.update({
        "occupation": {"concept_id": "occupation-example", "label": "Farmaceut/Receptarie",
                       "legacy_ams_taxonomy_id": "other-code"},
        "occupation_group": {"label": "Receptarier"},
        "occupation_field": {"label": "Hälso- och sjukvård"},
        "must_have": {"education_level": [{"label": "Gymnasial utbildning", "weight": 10}],
                      "skills": [{"label": "Excel"}]},
        "nice_to_have": {"education": [{"label": "Socialt arbete och omsorg", "weight": 5},
                                       {"label": "Socialt arbete och omsorg"}],
                         "work_experiences": [{"label": "Butikssäljare"}]},
    })
    job = ArbetsformedlingenScraper("arbetsformedlingen")._parse(row)
    assert job.occupation.model_dump() == {
        "label": "Farmaceut/Receptarie", "code": "occupation-example", "taxonomy": "JobTech Taxonomy",
    }
    assert job.department == "Hälso- och sjukvård"
    assert [(q.label, q.requirement) for q in job.education_requirements] == [
        ("Gymnasial utbildning", "required"), ("Socialt arbete och omsorg", "preferred"),
    ]


def test_jobtech_missing_or_unlabelled_concepts() -> None:
    row = _ads([datetime(2026, 11, 18)])[0]
    row.update({
        "occupation": {"concept_id": "unlabelled"}, "occupation_field": {"label": "Education"},
        "must_have": {"education": [{"concept_id": "x"}, {"label": " "}], "education_level": "3"},
        "nice_to_have": {"skills": [{"label": "Teaching"}]},
    })
    job = ArbetsformedlingenScraper("arbetsformedlingen")._parse(row)
    assert job.occupation is None and job.education_requirements is None


def test_native_skill_groups_set_priority_independently_of_weights() -> None:
    row = _ads([datetime(2026, 11, 18)])[0]
    row.update({
        "must_have": {"skills": [{"label": "C#", "weight": None}, {"label": " C# "}, {"concept_id": "s1"}],
                      "languages": [{"label": "English"}]},
        "nice_to_have": {"skills": [{"label": "SQL", "weight": 10}], "education": [{"label": "Diploma"}]},
    })
    job = ArbetsformedlingenScraper("any")._parse(row)
    assert [(s.label, s.requirement) for s in job.skill_requirements] == [
        ("C#", "required"), ("SQL", "preferred"),
    ]
    assert [(q.label, q.requirement) for q in job.education_requirements] == [("Diploma", "preferred")]
