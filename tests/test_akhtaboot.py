"""Tests for Akhtaboot."""

from __future__ import annotations

import re

import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType
from jobhive.scrapers import AkhtabootScraper, ScraperRegistry

_API_RE = re.compile(r"^https://www\.akhtaboot\.com/en/the-middle-east/jobs")

HTML_PAGE = """
<div class='job clearfix'>
  <div class='col-xs-12 col-sm-12 col-md-10 job-content'>
    <small class='col-md-3 pull-right'>
      Ref. Number: 166901
      <br>
      Date Posted: 11-05-2026
    </small>
    <a class='job-link' href='/en/jordan/jobs/amman/166901-Role-at-Acme' target=''>
      <h4>COMMUNICATIONS PROFESSIONAL</h4>
    </a>
    <p class='no-margin'>
      <strong>
        Norwegian Refugee Council (NRC)
        -
        <span>Amman, Jordan</span>
      </strong>
    </p>
  </div>
</div>
"""


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.akhtaboot as a

    monkeypatch.setattr(a, "MAX_RETRIES", 1)
    monkeypatch.setattr(a, "RETRY_BASE_DELAY", 0.0)


def test_registry_resolves_akhtaboot() -> None:
    assert ScraperRegistry.get(ATSType.AKHTABOOT) is AkhtabootScraper


def test_parses_akhtaboot_job_html(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, text=HTML_PAGE)
    httpx_mock.add_response(url=_API_RE, text="")

    jobs = AkhtabootScraper("any").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.ats_type is ATSType.AKHTABOOT
    assert j.ats_id == "166901"
    assert j.title == "COMMUNICATIONS PROFESSIONAL"
    assert j.company == "Norwegian Refugee Council (NRC)"
    assert j.location == "Amman, Jordan"
    assert str(j.url) == "https://www.akhtaboot.com/en/jordan/jobs/amman/166901-Role-at-Acme"
    assert j.posted_at is not None


def test_dedupes_repeated_pages(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, text=HTML_PAGE)
    httpx_mock.add_response(url=_API_RE, text=HTML_PAGE)

    jobs = AkhtabootScraper("any", max_pages=2).fetch()
    assert len(jobs) == 1


def test_500_raises(httpx_mock) -> None:
    httpx_mock.add_response(url=_API_RE, status_code=500, is_reusable=True)
    with pytest.raises(ScraperError):
        AkhtabootScraper("any").fetch()
