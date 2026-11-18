"""``WorkdayScraper.get_description`` reads one job at a time on one connection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers import workday
from ats_scrapers.scrapers.workday import WorkdayScraper

URL = "https://acme.wd5.myworkdayjobs.com/External"
DETAIL = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"


def _job(path: str | None = "/job/Paris/Engineer_R1", **fields: Any) -> Job:
    values: dict[str, Any] = {
        "url": f"{URL}{path or ''}",
        "title": "Engineer",
        "company": "Acme",
        "ats_type": ATSType.WORKDAY,
        "ats_id": "R1",
        "raw": {"externalPath": path} if path else None,
    }
    values.update(fields)
    return Job(**values)


class _Site:
    """Stands in for ``httpx.Client`` and records how it was used."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
        self.opened: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        real = httpx.Client

        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def client(**kwargs: Any) -> httpx.Client:
            self.opened.append(kwargs)
            return real(transport=httpx.MockTransport(answer))

        monkeypatch.setattr(workday.httpx, "Client", client)


def _detail(text: str) -> httpx.Response:
    return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": text}})


def test_descriptions_of_a_site_share_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _Site(monkeypatch, lambda request: _detail(f"<p>Role {request.url.path[-2:]}</p>"))
    scraper = WorkdayScraper(URL, proxy="http://user:pass@proxy.test:8080")

    first = scraper.get_description(_job("/job/Paris/Engineer_R1"))
    second = scraper.get_description(_job("/job/Lyon/Designer_R2"))

    assert (first, second) == ("Role R1", "Role R2")
    assert [str(request.url) for request in site.requests] == [
        f"{DETAIL}/job/Paris/Engineer_R1",
        f"{DETAIL}/job/Lyon/Designer_R2",
    ]
    assert len(site.opened) == 1
    assert site.opened[0]["proxy"] == "http://user:pass@proxy.test:8080"
    assert site.opened[0]["limits"].keepalive_expiry == workday.DETAIL_KEEPALIVE_SECONDS
    assert site.opened[0]["limits"].max_keepalive_connections == 8


def test_a_known_description_needs_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    site = _Site(monkeypatch, lambda request: _detail("unused"))

    assert WorkdayScraper(URL).get_description(_job(description="Already here")) == "Already here"
    assert site.opened == []


def test_the_path_falls_back_to_the_job_url(monkeypatch: pytest.MonkeyPatch) -> None:
    site = _Site(monkeypatch, lambda request: _detail("From the URL"))
    job = _job("/job/Paris/Engineer_R1", raw=None)

    assert WorkdayScraper(URL).get_description(job) == "From the URL"
    assert str(site.requests[0].url) == f"{DETAIL}/job/Paris/Engineer_R1"


def test_a_job_without_a_detail_path_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    site = _Site(monkeypatch, lambda request: _detail("unused"))

    assert WorkdayScraper(URL).get_description(_job(None)) is None
    assert site.requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, text="<html>challenge</html>"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["unexpected"]),
        httpx.Response(200, json={"jobPostingInfo": None}),
        httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "  "}}),
    ],
)
def test_an_unusable_answer_gives_no_description(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    _Site(monkeypatch, lambda request: response)

    assert WorkdayScraper(URL).get_description(_job()) is None


def test_a_failed_connection_gives_no_description(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _Site(monkeypatch, refuse)

    assert WorkdayScraper(URL).get_description(_job()) is None


def test_a_long_description_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    _Site(monkeypatch, lambda request: _detail("x" * 30_000))

    assert len(WorkdayScraper(URL).get_description(_job()) or "") == 25_000


def test_the_connection_closes_with_the_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    _Site(monkeypatch, lambda request: _detail("Role"))
    scraper = WorkdayScraper(URL)
    scraper.get_description(_job())
    client = scraper._detail_http
    assert client is not None and not client.is_closed

    del scraper

    assert client.is_closed


def test_close_releases_the_connection_and_can_be_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _Site(monkeypatch, lambda request: _detail("Role"))
    scraper = WorkdayScraper(URL)
    scraper.close()
    scraper.get_description(_job())
    client = scraper._detail_http
    assert client is not None

    scraper.close()
    scraper.close()

    assert client.is_closed
    assert scraper.get_description(_job()) == "Role"
    assert len(site.opened) == 2
    assert scraper._detail_http is not client


def test_several_jobs_can_be_read_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    site = _Site(monkeypatch, lambda request: _detail(f"Role {request.url.path[-2:]}"))
    scraper = WorkdayScraper(URL)
    jobs = [_job(f"/job/Paris/Engineer_R{number}") for number in range(1, 9)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(scraper.get_description, jobs))

    assert texts == [f"Role R{number}" for number in range(1, 9)]
    assert len(site.opened) == 1


def test_detail_preserves_paragraphs_and_literal_text(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(200, json={"jobPostingInfo": {
        "jobDescription": "<p>&nbsp;</p>",
        "externalJobDescription": "<p>Use C# &amp; SQL.</p><ul><li>Latency &lt; 5 ms.</li></ul>",
    }})
    site = _Site(monkeypatch, lambda request: response)
    job = _job()
    scraper = WorkdayScraper(URL)
    assert scraper.get_description(job) == "Use C# & SQL.\nLatency < 5 ms."
    assert scraper.get_description(job) == job.description
    assert len(site.requests) == 1


def test_entity_encoded_description_is_read_as_markup() -> None:
    info = {"jobDescription": "&lt;p&gt;Build APIs.&lt;/p&gt;"}
    assert workday._extract_description(info) == "Build APIs."


def test_entity_encoded_empty_description_falls_through_to_the_external_one() -> None:
    info = {"jobDescription": "&lt;p&gt;&lt;/p&gt;", "externalJobDescription": "<p>Ship it.</p>"}
    assert workday._extract_description(info) == "Ship it."
