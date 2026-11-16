"""Arbetsförmedlingen: walking past the 2,100-ad window of one query."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

from ats_scrapers.scrapers import ArbetsformedlingenScraper

_API_RE = re.compile(r"^https://jobsearch\.api\.jobtechdev\.se/search")


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


def test_empty_board(httpx_mock) -> None:
    httpx_mock.add_callback(_fake_api([]), url=_API_RE, is_reusable=True)

    assert ArbetsformedlingenScraper("arbetsformedlingen").fetch() == []
