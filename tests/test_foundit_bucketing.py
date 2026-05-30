"""Tests for Foundit's ``bucket_strategy="keyword"`` mode.

The opt-in keyword bucketing walks ~80 seed terms to bypass the
~9500-row deep-pagination cap on the ``/middleware/jobsearch``
endpoint. These tests pin:

  * The default ``bucket_strategy="none"`` keeps the old single-call
    no-query behaviour (full backwards-compat).
  * ``bucket_strategy="keyword"`` issues one walk per seed, passes
    the seed as ``query=``, dedupes across seeds, and respects the
    per-bucket 9500 cap.
  * Constructor validation rejects unknown strategies and dedupes
    user-supplied seed lists.

httpx is exercised via ``pytest-httpx``; we never hit the live site.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobhive.exceptions import ScraperError
from jobhive.scrapers import FounditScraper
from jobhive.scrapers import foundit as f_mod

_INDIA_RE = re.compile(
    r"^https://www\.foundit\.in/middleware/jobsearch"
)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op backoff so retry paths don't burn wall-clock time."""
    monkeypatch.setattr(f_mod, "_sleep_backoff", lambda attempt: None)
    monkeypatch.setattr(f_mod, "MAX_RETRIES", 2)


def _make_row(job_id: str, title: str = "Engineer") -> dict[str, Any]:
    """Minimal valid Foundit row — only the fields ``_parse_row``
    strictly requires plus a couple of nicities."""
    return {
        "id": job_id,
        "jobId": int(job_id) if job_id.isdigit() else 0,
        "title": title,
        "companyName": "Acme",
        "locations": "Mumbai",
        "jdUrl": f"/job/{title.lower()}-{job_id}",
        "currencyCode": "INR",
        "minimumSalary": {"absoluteValue": 100000},
        "maximumSalary": {"absoluteValue": 200000},
        "employmentTypes": ["Full time"],
        "minimumExperience": {"years": 1},
        "createdAt": 1_775_488_663_000,
    }


def _make_page(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "jobSearchStatus": 200,
        "jobSearchStatusText": "OK",
        "jobSearchResponse": {
            "data": list(rows),
            "meta": {
                "paging": {
                    "cursors": {"next": str(len(rows)), "previous": "0"},
                    "total": len(rows),
                    "limit": len(rows),
                },
            },
        },
    }


# --- constructor validation -------------------------------------------


def test_default_strategy_is_none() -> None:
    s = FounditScraper()
    assert s.bucket_strategy == "none"
    # Default seeds are populated but unused unless strategy is keyword.
    assert s.keyword_seeds == f_mod._default_keyword_seeds()


def test_unknown_bucket_strategy_raises() -> None:
    with pytest.raises(ScraperError, match="bucket_strategy"):
        FounditScraper(bucket_strategy="random")  # type: ignore[arg-type]


def test_keyword_seeds_override_strips_and_dedupes() -> None:
    s = FounditScraper(
        bucket_strategy="keyword",
        keyword_seeds=["engineer", "Engineer", "  ", "manager", "manager"],
    )
    # Order preserved, case-insensitive dedup, empties dropped.
    assert s.keyword_seeds == ("engineer", "manager")


def test_default_keyword_seeds_are_nonempty_and_unique() -> None:
    """Sanity-check the shipped seed list: at least 50 unique non-empty
    English seeds. A regression that empties / dupes the list would
    silently collapse coverage."""
    seeds = f_mod._default_keyword_seeds()
    assert len(seeds) >= 50
    assert all(s.strip() for s in seeds)
    # case-insensitive dedup
    lowered = [s.lower() for s in seeds]
    assert len(set(lowered)) == len(lowered)


# --- backwards-compat -------------------------------------------------


def test_none_strategy_uses_empty_query(httpx_mock: Any) -> None:
    """``bucket_strategy="none"`` keeps the original behaviour: one
    call sequence, ``query=`` empty. No regression for existing
    callers."""
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([_make_row("1")]))
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper("in", max_pages=5).fetch()
    assert [j.ats_id for j in jobs] == ["1"]
    requests = httpx_mock.get_requests()
    # Every request must carry an empty query in the original mode.
    assert all(r.url.params["query"] == "" for r in requests)


# --- keyword bucketing ------------------------------------------------


def test_keyword_strategy_passes_seed_as_query(httpx_mock: Any) -> None:
    """Each seed becomes a ``query=`` value. Distinct seeds yield
    distinct first-page requests with the right param."""
    # Two seeds, each returning one row then an empty page (stop).
    seeds = ("engineer", "manager")
    for kw in seeds:
        httpx_mock.add_response(
            url=_INDIA_RE,
            json=_make_page([_make_row(kw + "-1", title=kw)]),
        )
        httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper(
        "in", max_pages=5,
        bucket_strategy="keyword",
        keyword_seeds=list(seeds),
    ).fetch()

    assert {j.ats_id for j in jobs} == {"engineer-1", "manager-1"}
    queries = [r.url.params["query"] for r in httpx_mock.get_requests()]
    # First two pages of bucket A, then first two pages of bucket B.
    assert queries == ["engineer", "engineer", "manager", "manager"]


def test_keyword_strategy_dedupes_across_seeds(httpx_mock: Any) -> None:
    """A job hit by two seeds must appear once. Dedup is by ``ats_id``,
    not by row identity — the parser builds a fresh ``Job`` per row."""
    shared = _make_row("shared-1", title="Senior Engineer")
    unique_a = _make_row("only-a", title="Engineer")
    unique_b = _make_row("only-b", title="Manager")

    # engineer bucket returns shared + unique_a.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([shared, unique_a]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))
    # manager bucket returns shared (already seen) + unique_b.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([shared, unique_b]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper(
        "in", max_pages=5,
        bucket_strategy="keyword",
        keyword_seeds=["engineer", "manager"],
    ).fetch()
    assert [j.ats_id for j in jobs] == ["shared-1", "only-a", "only-b"]


def test_keyword_strategy_respects_per_bucket_offset_cap(
    httpx_mock: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 9500 offset cap applies *per bucket*. Verify it stops each
    walk independently instead of leaking into the next seed."""
    monkeypatch.setattr(f_mod, "MAX_USABLE_OFFSET", 150)

    # Bucket "engineer": pages at start=0 and start=100, then break.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("e1")]),
    )
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("e2")]),
    )
    # Bucket "manager": same two pages.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("m1")]),
    )
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("m2")]),
    )

    FounditScraper(
        "in", max_pages=99,
        bucket_strategy="keyword",
        keyword_seeds=["engineer", "manager"],
    ).fetch()

    reqs = httpx_mock.get_requests()
    starts_by_query: dict[str, list[str]] = {}
    for r in reqs:
        starts_by_query.setdefault(
            r.url.params["query"], [],
        ).append(r.url.params["start"])
    assert starts_by_query == {
        "engineer": ["0", "100"],
        "manager": ["0", "100"],
    }


def test_keyword_strategy_stops_bucket_on_repeat_window(
    httpx_mock: Any,
) -> None:
    """When a bucket returns the same rows on consecutive pages (the
    9500-cap wrap-around), the walker bails — and moves on to the
    next seed.

    A single zero-new page is no longer enough to abandon a bucket: an
    early page can be fully covered by an earlier seed while later pages
    still hold unique jobs, so the walker only stops after
    ``MAX_EMPTY_PAGES`` *consecutive* zero-new pages.
    """
    repeat_row = _make_row("r1")

    # Bucket 1: the same row served on the first page then on every
    # subsequent page → after the first page each page yields zero NEW
    # rows; stop once MAX_EMPTY_PAGES consecutive zero-new pages hit.
    for _ in range(1 + f_mod.MAX_EMPTY_PAGES):
        httpx_mock.add_response(
            url=_INDIA_RE, json=_make_page([repeat_row]),
        )
    # Bucket 2: one new row, then a truly empty page → immediate stop.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("b2")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper(
        "in", max_pages=10,
        bucket_strategy="keyword",
        keyword_seeds=["foo", "bar"],
    ).fetch()
    assert [j.ats_id for j in jobs] == ["r1", "b2"]
    queries = [r.url.params["query"] for r in httpx_mock.get_requests()]
    # "foo": page 0 yields the row, then MAX_EMPTY_PAGES consecutive
    # zero-new pages → stop. "bar": page 0 with a row, page 1 empty → stop.
    assert queries == (
        ["foo"] * (1 + f_mod.MAX_EMPTY_PAGES) + ["bar", "bar"]
    )


def test_keyword_strategy_continues_past_bucket_error(
    httpx_mock: Any,
) -> None:
    """A bucket that 502s for its retry budget must not crash the
    whole run — we still try the remaining seeds."""
    # Bucket "engineer": all 502 (2 attempts after retry override).
    httpx_mock.add_response(url=_INDIA_RE, status_code=502)
    httpx_mock.add_response(url=_INDIA_RE, status_code=502)
    # Bucket "manager": 1 good row + empty.
    httpx_mock.add_response(
        url=_INDIA_RE, json=_make_page([_make_row("m1")]),
    )
    httpx_mock.add_response(url=_INDIA_RE, json=_make_page([]))

    jobs = FounditScraper(
        "in", max_pages=5,
        bucket_strategy="keyword",
        keyword_seeds=["engineer", "manager"],
    ).fetch()
    assert [j.ats_id for j in jobs] == ["m1"]
