"""Amazon scraper: which link a job carries."""

from __future__ import annotations

import re

from ats_scrapers.scrapers.amazon import AmazonScraper


def _hit(**fields: object) -> dict[str, object]:
    return {"id_icims": "10605398", "title": "Area Manager", **fields}


def test_job_url_is_the_public_job_page() -> None:
    [job] = AmazonScraper("amazon")._parse_hit(_hit(
        job_path="/en/jobs/10605398/area-manager",
        url_next_step="https://account.amazon.jobs/jobs/10605398/apply",
    ))

    assert str(job.url) == "https://www.amazon.jobs/en/jobs/10605398/area-manager"
    assert str(job.apply_url) == "https://account.amazon.jobs/jobs/10605398/apply"


def test_job_url_falls_back_to_the_next_step_then_the_job_id() -> None:
    [with_step] = AmazonScraper("amazon")._parse_hit(_hit(
        url_next_step="https://account.amazon.jobs/jobs/10605398/apply",
    ))
    [bare] = AmazonScraper("amazon")._parse_hit(_hit())

    assert str(with_step.url) == "https://account.amazon.jobs/jobs/10605398/apply"
    assert str(bare.url) == "https://www.amazon.jobs/en/jobs/10605398"


def _facets(found: int, categories: list[tuple[str, int]]) -> dict[str, object]:
    return {
        "found": found,
        "facets": [{
            "name": "businessCategory",
            "values": [{"name": name, "count": count} for name, count in categories],
        }],
    }


def test_buckets_by_category_when_found_stops_at_the_window(httpx_mock) -> None:
    # ``found`` reads 10,000 while the facet counts add up to 11,000.
    httpx_mock.add_response(
        method="POST",
        url="https://www.amazon.jobs/api/jobs/search",
        json=_facets(10_000, [("aws", 6_000), ("retail", 5_000)]),
    )
    for slug, job_id in (("aws", "1"), ("retail", "2")):
        httpx_mock.add_response(
            method="GET",
            url=re.compile(
                r"^https://www\.amazon\.jobs/en/search\.json\?"
                rf"business_category%5B%5D={slug}&result_limit=100&offset=0$"
            ),
            json={"hits": 1, "jobs": [_hit(id_icims=job_id)]},
        )

    jobs = AmazonScraper("amazon").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2"]


def test_small_catalogue_is_paginated_without_buckets(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://www.amazon.jobs/api/jobs/search",
        json=_facets(150, [("aws", 150)]),
    )
    for offset, job_id in ((0, "1"), (100, "2")):
        httpx_mock.add_response(
            method="GET",
            url=(
                "https://www.amazon.jobs/en/search.json"
                f"?result_limit=100&offset={offset}"
            ),
            json={"hits": 150, "jobs": [_hit(id_icims=job_id)]},
        )

    jobs = AmazonScraper("amazon").fetch()

    assert sorted(job.ats_id for job in jobs) == ["1", "2"]
