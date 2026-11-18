"""Tests for the Workable XML feed scraper.

Pin the grouping of a multi-location posting into one job (entries of one
shortcode are not always adjacent), the field mapping, and the download.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import (
    ScraperRegistry,
    WorkableFeedScraper,
    WorkableScraper,
    workable_feed,
)

FEED_URL = "https://feed.example.com/jobs.xml"


def _entry(shortcode: str, title: str, city: str, state: str, country: str, *,
           remote: str = "false", date: str = "Fri, 13 Nov 2026 16:20:57 UTC",
           jobtype: str = "Full-time", category: str = "", education: str = "",
           experience: str = "", company: str = "KeystoneCare") -> str:
    fields = {
        "title": title, "date": date, "referencenumber": shortcode,
        "url": f"https://apply.workable.com/j/{shortcode}", "company": company,
        "city": city, "state": state, "country": country, "remote": remote,
        "postalcode": "19151",
        "description": "<p>Expand access to <strong>care</strong>.</p><p><strong>Requirements</strong></p><ul><li>RN</li></ul>",
        "education": education, "jobtype": jobtype, "category": category,
        "experience": experience, "website": "https://www.keystonecare.com/",
    }
    body = "".join(
        f"    <{k}>\n      <![CDATA[{v}]]>\n    </{k}>\n" for k, v in fields.items()
    )
    return f"  <job>\n{body}  </job>\n"


def _feed(*entries: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<source>\n'
        "  <publisher>Workable</publisher>\n"
        "  <publisherurl>https://www.workable.com</publisherurl>\n"
        + "".join(entries)
        + "</source>\n"
    )


FEED = _feed(
    _entry("5A286F5DE5", "Account Executive", "Philadelphia", "PA", "US",
           category="Sales", education="Bachelor's Degree", experience="Mid-Senior level"),
    _entry("5A286F5DE5", "Account Executive", "Roslyn", "PA", "US",
           date="Fri, 13 Nov 2026 16:20:58 UTC"),
    _entry("F44ED9E40A", "System Engineer", "Hyderabad", "Telangana", "IN",
           jobtype="Contract", date="Wed, 28 Jul 2021 10:24:17 UTC"),
    _entry("5A286F5DE5", "Account Executive", "", "", "CA", remote="true",
           date="Fri, 13 Nov 2026 16:21:30 UTC"),
    _entry("0908074E5D", "Carwash Detailer", "Albuquerque", "NM", "US", jobtype="Other"),
)


@pytest.fixture
def feed_file(tmp_path: Path) -> Path:
    path = tmp_path / "feed.xml"
    path.write_text(FEED)
    return path


def test_registry_keeps_the_board_scraper() -> None:
    assert ScraperRegistry.get(ATSType.WORKABLE) is WorkableScraper


def test_groups_a_posting_listed_once_per_location(feed_file: Path) -> None:
    jobs = WorkableFeedScraper(feed_path=feed_file).fetch()

    assert [j.ats_id for j in jobs] == ["5A286F5DE5", "F44ED9E40A", "0908074E5D"]
    job = jobs[0]
    assert job.ats_type is ATSType.WORKABLE
    assert job.global_id == "workable:5A286F5DE5"
    assert str(job.url) == "https://apply.workable.com/j/5A286F5DE5"
    assert job.apply_url is None
    assert job.location == "Philadelphia, PA, US | Roslyn, PA, US | CA"
    assert [(p.city, p.region, p.country_iso, p.is_primary) for p in job.locations] == [
        ("Philadelphia", "PA", "US", None), ("Roslyn", "PA", "US", None),
        (None, None, "CA", None),
    ]
    assert job.workplace_type is None
    # Two countries: left to the location text.
    assert job.country_iso is None
    # One remote entry makes the posting remote.
    assert job.is_remote is True
    assert job.posted_at == datetime(2026, 11, 13, 16, 20, 57, tzinfo=UTC)
    assert job.company == "KeystoneCare"
    assert job.department == "Sales"
    assert job.job_category == "Sales"
    assert [(q.label, q.requirement) for q in job.education_requirements] == [
        ("Bachelor's Degree", "unspecified"),
    ]
    assert job.employment_type == "FULL_TIME" and job.commitment == "Full-time"
    assert job.description is not None and job.description.startswith("<p>Expand access")
    assert "<strong>Requirements</strong>" in job.description
    assert job.raw == {
        "education": "Bachelor's Degree",
        "experience": "Mid-Senior level",
        "website": "https://www.keystonecare.com/",
    }


def test_single_location_fields(feed_file: Path) -> None:
    jobs = {j.ats_id: j for j in WorkableFeedScraper(feed_path=feed_file).fetch()}

    engineer = jobs["F44ED9E40A"]
    assert engineer.location == "Hyderabad, Telangana, IN"
    assert engineer.country_iso == "IN"
    assert engineer.is_remote is False
    assert engineer.employment_type == "CONTRACT"
    assert engineer.department is None
    assert engineer.posted_at == datetime(2021, 7, 28, 10, 24, 17, tzinfo=UTC)
    detailer = jobs["0908074E5D"]
    assert detailer.employment_type is None and detailer.commitment == "Other"


def test_descriptions_can_be_skipped(feed_file: Path) -> None:
    jobs = WorkableFeedScraper(feed_path=feed_file, include_descriptions=False).fetch()
    assert all(j.description is None for j in jobs)


@pytest.mark.parametrize(
    "content",
    [FEED[: len(FEED) // 2], "", _feed()],
    ids=["truncated", "empty file", "no jobs"],
)
def test_broken_feed_raises(tmp_path: Path, content: str) -> None:
    path = tmp_path / "feed.xml"
    path.write_text(content)
    with pytest.raises(ScraperError):
        WorkableFeedScraper(feed_path=path).fetch()


def test_downloads_the_feed_to_a_temporary_file(
    httpx_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workable_feed.tempfile, "tempdir", str(tmp_path))
    httpx_mock.add_response(url=FEED_URL, content=FEED.encode())

    jobs = WorkableFeedScraper(feed_url=FEED_URL).fetch()

    assert len(jobs) == 3
    assert list(tmp_path.iterdir()) == []


def test_download_retries_a_server_error(httpx_mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workable_feed, "RETRY_DELAY", 0)
    httpx_mock.add_response(url=FEED_URL, status_code=503)
    httpx_mock.add_response(url=FEED_URL, content=FEED.encode())

    assert len(WorkableFeedScraper(feed_url=FEED_URL).fetch()) == 3


def test_encoded_download_skips_the_length_check(httpx_mock) -> None:
    # Content-Length counts encoded bytes, so it cannot be compared with the
    # decoded file.
    body = FEED.encode()
    httpx_mock.add_response(
        url=FEED_URL,
        content=body,
        headers={"Content-Encoding": "identity", "Content-Length": str(len(body) + 50)},
    )

    assert len(WorkableFeedScraper(feed_url=FEED_URL).fetch()) == 3


def test_short_download_is_retried_then_rejected(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workable_feed, "RETRY_DELAY", 0)
    body = FEED.encode()
    for _ in range(workable_feed.DOWNLOAD_ATTEMPTS):
        httpx_mock.add_response(
            url=FEED_URL,
            content=body[:100],
            headers={"Content-Length": str(len(body))},
        )

    with pytest.raises(ScraperError, match="download stopped at 100 of"):
        WorkableFeedScraper(feed_url=FEED_URL).fetch()


def test_feed_address_from_the_environment(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(workable_feed.FEED_URL_ENV, FEED_URL)
    httpx_mock.add_response(url=FEED_URL, content=FEED.encode())

    assert len(WorkableFeedScraper().fetch()) == 3


def test_missing_feed_address_says_how_to_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(workable_feed.FEED_URL_ENV, raising=False)
    with pytest.raises(ScraperError, match="ask Workable support"):
        WorkableFeedScraper().fetch()


def test_download_waits_for_retry_after(
    httpx_mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(workable_feed.asyncio, "sleep", record)
    httpx_mock.add_response(url=FEED_URL, status_code=429, headers={"Retry-After": "45"})
    httpx_mock.add_response(url=FEED_URL, content=FEED.encode())

    assert len(WorkableFeedScraper(feed_url=FEED_URL).fetch()) == 3
    assert 45.0 in waits


def test_long_retry_after_fails_at_once(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, status_code=429, headers={"Retry-After": "3600"})
    with pytest.raises(ScraperError, match="Retry-After 3600s"):
        WorkableFeedScraper(feed_url=FEED_URL).fetch()


def test_dates_do_not_depend_on_the_locale() -> None:
    import locale

    saved = locale.setlocale(locale.LC_TIME)
    for name in ("fr_FR.UTF-8", "fr_FR.utf8", "de_DE.UTF-8", "de_DE.utf8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no French or German locale installed")
    try:
        assert workable_feed._parse_date("Wed, 28 Jul 2021 10:24:17 UTC") == datetime(
            2021, 7, 28, 10, 24, 17, tzinfo=UTC
        )
    finally:
        locale.setlocale(locale.LC_TIME, saved)


def test_download_refused(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, status_code=403)
    with pytest.raises(ScraperError, match="HTTP 403"):
        WorkableFeedScraper(feed_url=FEED_URL).fetch()



def test_empty_education_does_not_promote_requirements_prose(tmp_path: Path) -> None:
    path = tmp_path / "feed.xml"
    path.write_text(_feed(_entry("1", "Education lead", "Rome", "", "IT",
                                category="Other", education="Unspecified")))
    job, = WorkableFeedScraper(feed_path=path).fetch()
    assert "Requirements" in job.description
    assert job.job_category is None and job.education_requirements is None
