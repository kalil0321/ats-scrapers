"""Tests for the jobs.ch scraper.

Pin the parsing contract, the offset-based pagination plan
(``total_hits / per_page``), and the ``_links.detail_*`` URL
preference (we want the localized URL when present, fall back to a
constructed canonical English URL otherwise).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import JobsChScraper, ScraperRegistry

_API_RE = re.compile(r"^https://www\.jobs\.ch/api/v1/public/search(?:\?|$)")


# Retry pacing is zeroed suite-wide by the `_no_retry_delays` fixture
# in conftest.py — the shared fetch layer replaced the per-scraper
# retry constants.
@pytest.fixture(autouse=True)
def _detail_page_stub(httpx_mock) -> None:
    def default_detail(request):
        return httpx.Response(200, json={
            "job_id": request.url.path.split("/")[-1],
            "template_text": "<div>Build Swiss products.</div>",
        })

    httpx_mock.add_callback(default_detail, url=re.compile(
        r"^https://www\.jobs\.ch/api/v1/public/search/job/(?!meta-1$|detail-test$|gone$)"
    ), is_reusable=True, is_optional=True)


def _doc(
    *,
    job_id: str,
    title: str,
    company: str = "Acme AG",
    place: str = "Zürich",
    grades: list[int] | None = None,
    languages: list[str] | None = None,
    detail_de: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job_id,
        "title": title,
        "company_name": company,
        "company_id": 1234,
        "place": place,
        "is_active": True,
        "is_paid": True,
        "publication_date": "2026-05-07T10:57:08+02:00",
        "employment_grades": grades if grades is not None else [100],
        "language_skills": [
            {"language": lang, "level": 1} for lang in (languages or ["de"])
        ],
        "company_segmentation": "gu",
    }
    if detail_de:
        out["_links"] = {"detail_de": {"href": detail_de, "templated": False}}
    return out


def _page(docs: list[dict], total_hits: int) -> dict:
    return {"documents": docs, "total_hits": total_hits, "start": 0, "rows": 20}


# --- registry ---------------------------------------------------------------


def test_registry_resolves_jobsch() -> None:
    assert ScraperRegistry.get(ATSType.JOBSCH) is JobsChScraper


# --- happy path -------------------------------------------------------------


def test_parses_full_doc(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(
            job_id="abc-123",
            title="Senior Software Engineer",
            company="Acme AG",
            place="Zürich",
            grades=[100],
            languages=["de", "en"],
            detail_de="https://www.jobs.ch/de/stellenangebote/detail/abc-123/",
        )], total_hits=1),
    )
    jobs = JobsChScraper("any", query_seeds=()).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.JOBSCH
    assert j.ats_id == "abc-123"
    assert j.title == "Senior Software Engineer"
    assert j.company == "Acme AG"
    assert j.location == "Zürich, Switzerland"
    assert j.employment_type == "FULL_TIME"
    assert j.posted_at is not None
    assert j.raw is not None
    assert j.raw["languages"] == ["de", "en"]
    assert j.raw["employment_grades"] == [100]
    assert j.description == "Build Swiss products."
    # _links.detail_de is preferred over the constructed fallback URL.
    assert str(j.url) == "https://www.jobs.ch/de/stellenangebote/detail/abc-123/"


def test_skips_publication_notice_when_api_and_page_have_no_body(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _doc(
                job_id="meta-1",
                title="Engineer",
                detail_de="https://example.com/jobs/meta-1/",
            )
        ], total_hits=1),
    )
    httpx_mock.add_response(url="https://www.jobs.ch/api/v1/public/search/job/meta-1", json={})
    httpx_mock.add_response(
        url="https://example.com/jobs/meta-1/",
        text=(
            "<html><head><meta content='Apply now for this job!' "
            "name='description'></head></html>"
        ),
    )

    jobs = JobsChScraper("any", query_seeds=()).fetch()

    assert jobs[0].description is None


def test_falls_back_to_canonical_url_when_no_links(httpx_mock) -> None:
    """Some rows ship without ``_links``; build a canonical URL from the
    job_id rather than emit a half-broken row."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="xyz-7", title="Engineer")], total_hits=1),
    )
    j = JobsChScraper("any", query_seeds=()).fetch()[0]
    assert str(j.url) == "https://www.jobs.ch/en/vacancies/detail/xyz-7/"


def test_part_time_employment_type(httpx_mock) -> None:
    """All grades < 100 → PART_TIME."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="p1", title="X", grades=[50])], total_hits=1),
    )
    assert JobsChScraper("any", query_seeds=()).fetch()[0].employment_type == "PART_TIME"


def test_mixed_grades_no_employment_type(httpx_mock) -> None:
    """[80, 100] is 'flexible' — leave employment_type None rather than
    pick one arbitrarily."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="p1", title="X", grades=[80, 100])], total_hits=1),
    )
    assert JobsChScraper("any", query_seeds=()).fetch()[0].employment_type is None


# --- pagination -------------------------------------------------------------


def test_paginates_total_hits_into_offsets(httpx_mock) -> None:
    """First request reads ``total_hits``; subsequent fan-out requests
    cover ``[20, 40, 60, …]`` until the total is consumed."""
    # 50 total → 3 pages of 20 (last page short).
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"a{i}", title=f"Job {i}") for i in range(20)],
                   total_hits=50),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"b{i}", title=f"Job {i}") for i in range(20)],
                   total_hits=50),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"c{i}", title=f"Job {i}") for i in range(10)],
                   total_hits=50),
    )
    jobs = JobsChScraper("any", query_seeds=()).fetch()
    assert len(jobs) == 50


def test_max_pages_truncates(httpx_mock) -> None:
    """Even if ``total_hits`` says 1000, ``max_pages=2`` must cap the
    fan-out at 1 follow-up page (probe = 1, total page-fetches = 2)."""
    # Probe page (start=0) → 20 unique ids a0..a19
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"a{i}", title="X") for i in range(20)],
                   total_hits=1000),
    )
    # Fan-out page (start=20) → 20 different ids b0..b19. After
    # max_pages=2 cap we must NOT issue a 3rd request — if we do,
    # httpx_mock errors with 'no response' for the un-stubbed call.
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id=f"b{i}", title="X") for i in range(20)],
                   total_hits=1000),
    )
    jobs = JobsChScraper("any", max_pages=2, query_seeds=()).fetch()
    assert len(jobs) == 40


def test_no_fanout_when_total_under_per_page(httpx_mock) -> None:
    """If total_hits ≤ 20, just keep the first response and stop."""
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="solo", title="Only")], total_hits=1),
    )
    jobs = JobsChScraper("any", query_seeds=()).fetch()
    assert len(jobs) == 1


def test_enriches_after_cross_seed_dedupe(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="dupe", title="Engineer")], total_hits=1),
    )
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([_doc(job_id="dupe", title="Engineer")], total_hits=1),
    )

    jobs = JobsChScraper("any", query_seeds=("engineer",)).fetch()

    assert [j.ats_id for j in jobs] == ["dupe"]
    detail_requests = httpx_mock.get_requests(
        url=re.compile(r"^https://www\.jobs\.ch/api/v1/public/search/job/dupe$")
    )
    assert len(detail_requests) == 1


def _fake_search(docs: list[dict[str, Any]]):
    """Serve ``docs`` with the API's date filter and 2,000-row window.

    Each doc's ``_published`` is the local ``Y-m-d H:i:s`` time the
    ``publication-date-from`` (inclusive) / ``-to`` (exclusive) pair filters on.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        start = int(params.get("start", 0))
        if start >= 2000:
            return httpx.Response(422, json={"errors": []})
        lo = params.get("publication-date-from")
        hi = params.get("publication-date-to")
        matches = [
            d for d in docs
            if lo is None or lo <= d["_published"] < hi
        ]
        return httpx.Response(200, json=_page(matches[start:start + 20], len(matches)))

    return respond


def _dated_docs(count: int) -> list[dict[str, Any]]:
    """``count`` postings first published one minute apart from 2026-01-01."""
    first = datetime(2026, 1, 1)
    docs = []
    for i in range(count):
        doc = _doc(job_id=f"j{i}", title=f"Job {i}")
        doc["_published"] = (first + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        docs.append(doc)
    return docs


def test_splits_publication_dates_past_the_row_window(httpx_mock) -> None:
    httpx_mock.add_callback(_fake_search(_dated_docs(4_500)), url=_API_RE, is_reusable=True)

    jobs = JobsChScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 4_500


# --- 403 blocks -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_block_pauses(monkeypatch) -> None:
    from ats_scrapers.scrapers import jobsch

    monkeypatch.setattr(jobsch, "BLOCK_PAUSES", (0.0,) * 5)
    monkeypatch.setattr(jobsch, "PACED_RATES", (1e6,))
    monkeypatch.delenv("ATS_SCRAPERS_PROXY", raising=False)
    monkeypatch.delenv("PROXY", raising=False)


@pytest.fixture
def _proxy_env(monkeypatch) -> None:
    monkeypatch.setenv("PROXY", "http://proxy.example:8080:user:secret")


def _blocking(serve, refuse):
    """Answer 403 when ``refuse(params, index, repeat)`` holds, else ``serve``.

    ``index`` counts every earlier request, ``repeat`` the earlier ones with
    the same query string.
    """
    asked: dict[str, int] = {}
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        key = str(request.url.query)
        index, repeat = count, asked.get(key, 0)
        count += 1
        asked[key] = repeat + 1
        if refuse(params, index, repeat):
            return httpx.Response(403, text="<html>blocked</html>")
        return serve(request)

    return respond


def test_window_refused_through_the_proxy_is_retried(httpx_mock, _proxy_env) -> None:
    """The direct probe is refused, then two asks for each newer window's
    first page: the windows that hold most of the board are read anyway."""

    def refuse(params: dict[str, str], index: int, repeat: int) -> bool:
        newer_window = "2026-01-02" <= params.get("publication-date-from", "") < "2026-02"
        return index == 0 or (newer_window and params["start"] == "0" and repeat < 2)

    respond = _blocking(_fake_search(_dated_docs(4_500)), refuse)
    httpx_mock.add_callback(respond, url=_API_RE, is_reusable=True)

    jobs = JobsChScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 4_500


def test_page_refused_through_the_proxy_is_retried(httpx_mock, _proxy_env) -> None:
    def refuse(params: dict[str, str], index: int, repeat: int) -> bool:
        return index == 0 or (params["start"] == "40" and repeat == 0)

    respond = _blocking(_fake_search(_dated_docs(4_500)), refuse)
    httpx_mock.add_callback(respond, url=_API_RE, is_reusable=True)

    jobs = JobsChScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 4_500


def test_block_that_outlasts_every_pause_fails_the_fetch(httpx_mock, _proxy_env) -> None:
    from ats_scrapers.scrapers import jobsch

    def refuse(params: dict[str, str], index: int, repeat: int) -> bool:
        return index == 0 or "publication-date-from" in params

    respond = _blocking(_fake_search(_dated_docs(4_500)), refuse)
    httpx_mock.add_callback(respond, url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="for the block to lift"):
        JobsChScraper("any", include_descriptions=False).fetch()

    dated = [
        r for r in httpx_mock.get_requests(url=_API_RE)
        if b"publication-date-from" in r.url.query
    ]
    assert len(dated) == len(jobsch.BLOCK_PAUSES) + 1


def test_block_after_the_first_answer_is_waited_out_without_a_proxy(httpx_mock) -> None:
    def refuse(params: dict[str, str], index: int, repeat: int) -> bool:
        return 150 <= index < 155

    respond = _blocking(_fake_search(_dated_docs(4_500)), refuse)
    httpx_mock.add_callback(respond, url=_API_RE, is_reusable=True)

    jobs = JobsChScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 4_500


def test_request_in_flight_at_a_403_is_not_resent_during_the_pause(
    httpx_mock, monkeypatch,
) -> None:
    """Pages 20 to 80 go out together: 20 is refused while 40 still waits for
    its answer. Nothing may reach the site until the pause is over."""
    from ats_scrapers import fetch as fetch_module
    from ats_scrapers.scrapers import jobsch

    monkeypatch.setattr(jobsch, "BLOCK_PAUSES", (0.5,))
    waiting: set[asyncio.Future[None]] = set()
    real_aclose = fetch_module.Fetcher.aclose

    async def aclose(self) -> None:
        # Closing the pool drops every connection still waiting for an answer.
        for answer in waiting:
            if not answer.done():
                answer.set_exception(httpx.ReadError("connection closed"))
        await real_aclose(self)

    monkeypatch.setattr(fetch_module.Fetcher, "aclose", aclose)
    serve = _fake_search(_dated_docs(100))
    sent: list[float] = []
    refused_at: list[float] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        loop = asyncio.get_running_loop()
        sent.append(loop.time())
        start = parse_qs(urlsplit(str(request.url)).query)["start"][0]
        if start == "20" and not refused_at:
            await asyncio.sleep(0.05)
            refused_at.append(loop.time())
            return httpx.Response(403, text="<html>blocked</html>")
        if start == "40" and len(sent) <= 5:
            answer = loop.create_future()
            waiting.add(answer)
            loop.call_later(0.2, lambda: answer.done() or answer.set_result(None))
            try:
                await answer
            finally:
                waiting.discard(answer)
        return serve(request)

    httpx_mock.add_callback(respond, url=_API_RE, is_reusable=True)

    jobs = JobsChScraper("any", include_descriptions=False).fetch()

    assert len(jobs) == 100
    during_pause = [t for t in sent if refused_at[0] < t < refused_at[0] + 0.4]
    assert during_pause == []


def test_refused_first_request_without_a_proxy_raises_at_once(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=403, is_reusable=True)

    with pytest.raises(ScraperError, match="no PROXY env var is set"):
        JobsChScraper("any", include_descriptions=False).fetch()

    assert len(httpx_mock.get_requests(url=_API_RE)) == 1


async def test_pauses_grow_until_a_request_sent_after_one_is_answered(monkeypatch) -> None:
    from ats_scrapers.scrapers import jobsch

    monkeypatch.setattr(jobsch, "BLOCK_PAUSES", (1.0, 2.0, 3.0))
    monkeypatch.setattr(jobsch, "PACED_RATES", (2.0, 1.0))
    now = asyncio.get_running_loop().time
    throttle = jobsch._Throttle()

    assert throttle.pause() == 1.0
    assert throttle.paused_since(-1.0)
    throttle.passed(sent_at=-1.0)
    assert throttle.pause() == 2.0
    assert throttle.interval == 0.5

    throttle.passed(sent_at=now())
    assert throttle.pause() == 1.0
    assert throttle.interval == 1.0
    assert (throttle.pause(), throttle.pause()) == (2.0, 3.0)
    assert throttle.spent


def _undated_board(date_status: int = 200):
    """A 5,000-posting board whose search ignores, or rejects, the date filter."""
    docs = [_doc(job_id=f"j{i}", title=f"Job {i}") for i in range(5_000)]

    def respond(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        start = int(params.get("start", 0))
        if start >= 2000 or ("publication-date-from" in params and date_status != 200):
            return httpx.Response(422, json={"errors": []})
        return httpx.Response(200, json=_page(docs[start:start + 20], len(docs)))

    return respond


def test_search_that_ignores_the_date_filter_raises(httpx_mock) -> None:
    httpx_mock.add_callback(_undated_board(), url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="no longer filters by publication date"):
        JobsChScraper("any", include_descriptions=False).fetch()

    assert len(httpx_mock.get_requests()) < 200


def test_search_that_rejects_a_date_window_raises(httpx_mock) -> None:
    httpx_mock.add_callback(_undated_board(date_status=422), url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="rejected the search"):
        JobsChScraper("any", include_descriptions=False).fetch()


def test_date_walk_stops_at_its_window_ceiling(httpx_mock, monkeypatch) -> None:
    from ats_scrapers.scrapers import jobsch

    monkeypatch.setattr(jobsch, "MAX_DATE_WINDOWS", 3)
    httpx_mock.add_callback(_fake_search(_dated_docs(4_500)), url=_API_RE, is_reusable=True)

    with pytest.raises(ScraperError, match="passed 3 windows"):
        JobsChScraper("any", include_descriptions=False).fetch()


# --- defensive --------------------------------------------------------------


def test_skips_doc_missing_id_or_title(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_API_RE,
        json=_page([
            _doc(job_id="ok", title="Good"),
            {"job_id": "no-title"},
            {"title": "no-id"},
        ], total_hits=3),
    )
    jobs = JobsChScraper("any", query_seeds=()).fetch()
    assert [j.ats_id for j in jobs] == ["ok"]


def test_persistent_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        JobsChScraper("any", query_seeds=()).fetch()


@pytest.mark.parametrize("defer", [True, False])
def test_detail_returns_original_sections(httpx_mock, defer) -> None:
    doc = _doc(job_id="detail-test", title="Mitarbeiter")
    doc.update(preview="SEO teaser...")
    httpx_mock.add_response(url=_API_RE, json=_page([doc], 1))
    httpx_mock.add_response(url="https://www.jobs.ch/api/v1/public/search/job/detail-test", json={
        "job_id": "detail-test", "template_text": "<h2>Aufgaben</h2><p>C++ &amp; C#.</p>",
        "template_lead_text": "Unser Team", "template_contact_address": "<p>Kontakt</p>",
    })
    scraper = JobsChScraper("any", include_descriptions=not defer)
    job, = scraper.fetch()
    if defer:
        assert job.description is None and len(httpx_mock.get_requests()) == 1
    assert scraper.get_description(job) == "Unser Team\n\nAufgaben\nC++ & C#.\n\nKontakt"
    assert len(httpx_mock.get_requests()) == 2


def test_html_fallback_prefers_original_and_preserves_nested_sections(httpx_mock) -> None:
    scraper = JobsChScraper("any")
    job = scraper._parse(_doc(job_id="detail-test", title="Engineer"))
    httpx_mock.add_response(url="https://www.jobs.ch/api/v1/public/search/job/detail-test",
                            json={"job_id": "detail-test", "preview": "Teaser"})
    httpx_mock.add_response(url=str(job.url), text="""
      <script type="application/ld+json">{"@type":"JobPosting","description":"Translation"}</script>
      <section data-cy="vacancy-description"><div data-cy="info-block">Summary</div>
      <h4>About the job</h4><div>Original <b>text</b>.</div><p>Last paragraph.</p></section>""")
    assert scraper.get_description(job) == "Original text.\nLast paragraph."


def test_listing_body_needs_no_detail_request(httpx_mock) -> None:
    doc = _doc(job_id="inline", title="Tester")
    doc.update(template_text="<p>Test it.</p>")
    httpx_mock.add_response(url=_API_RE, json=_page([doc], 1))
    job, = JobsChScraper("any").fetch()
    assert job.description == "Test it."
    assert len(httpx_mock.get_requests()) == 1


def test_complete_template_in_api_avoids_page_fetch(httpx_mock) -> None:
    scraper = JobsChScraper("any")
    job = scraper._parse(_doc(job_id="detail-test", title="Tester"))
    httpx_mock.add_response(url="https://www.jobs.ch/api/v1/public/search/job/detail-test", json={
        "job_id": "detail-test", "template": "<html><head><title>SEO</title></head>"
        "<body><p>Test systems.</p><ul><li>Collaborate.</li></ul></body></html>",
    })
    assert scraper.get_description(job) == "Test systems.\nCollaborate."
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(("grades", "ids", "percentages", "engagement"), [
    ([80, 85, 90, 95, 100], ["5"], [80, 85, 90, 95, 100], "permanent"),
    ([80, 100], ["1"], [80, 100], "temporary"),
    ([62.5, 0, 62.5], [2], [62.5, 0], "independent_contractor"),
    ([100], ["3"], [100], "internship"),
    ([60], ["6"], [60], "apprenticeship"),
    ("80–100%", ["4"], None, None),
    ([20, True], ["5", "1"], None, None),
    ([101], ["5", "unknown"], None, None),
    ([-1], [], None, None),
])
def test_jobsch_normalizes_explicit_choices_only(grades, ids, percentages, engagement):
    item = {**_doc(job_id="a", title="Engineer"), "employment_grades": grades,
            "employment_type_ids": ids}
    job = JobsChScraper("any")._parse(item)
    assert job is not None
    assert job.workload_percentages == percentages
    assert job.engagement_type == engagement
    assert job.raw["employment_grades"] == grades
    if ids:
        assert job.raw["employment_type_ids"] == ids


def test_jobsch_reads_detail_only_fields_in_existing_request(httpx_mock):
    scraper = JobsChScraper("any")
    job = scraper._parse(_doc(job_id="detail-test", title="Engineer"))
    httpx_mock.add_response(json={"job_id": "detail-test", "template_text": "Build systems.",
                                  "employment_type_ids": ["5"], "employment_grades": [60, 80]})
    assert scraper.get_description(job) == "Build systems."
    assert (job.engagement_type, job.employment_type) == ("permanent", "PART_TIME")
    assert job.workload_percentages == [60, 80]
    assert len(httpx_mock.get_requests()) == 1
