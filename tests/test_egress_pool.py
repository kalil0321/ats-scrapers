from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

import scripts.run_pipeline as runner
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType, Job


def test_pool_parses_proxies_and_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_EGRESS_POOL", "direct, http://u:p@a:1\nhttp://u:p@b:2,")
    assert runner._egress_pool() == [None, "http://u:p@a:1", "http://u:p@b:2"]


def test_no_pool_means_a_single_direct_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATS_SCRAPERS_EGRESS_POOL", raising=False)
    assert runner._egress_attempts(runner._egress_pool()) == [None]


def test_tenants_take_turns_and_retry_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ["http://a", "http://b", "http://c", "http://d"]
    assert runner._egress_attempts(["http://a", "http://b"])[:2] in (["http://a", "http://b"], ["http://b", "http://a"])
    monkeypatch.setenv("ATS_SCRAPERS_FALLBACK_PROXY", "http://residential")
    firsts = []
    for _ in range(8):
        attempts = runner._egress_attempts(pool)
        assert attempts[0] != attempts[1]
        assert attempts[-1] == "http://residential"
        firsts.append(attempts[0])
    assert set(firsts) == set(pool)


class _Scraper:
    calls: ClassVar[list[str | None]] = []

    def __init__(self, slug, *, timeout=30, proxy=None, **_kw) -> None:
        self.slug, self.proxy = slug, proxy
        self.include_descriptions = True

    def fetch(self):
        type(self).calls.append(self.proxy)
        if self.slug == "gone":
            raise CompanyNotFoundError("missing")
        if self.proxy != "http://b":
            raise ScraperError("returned 429 after 3 attempts")
        return [Job(url="https://x/1", title="T", company="C", ats_type=ATSType.CUSTOM, ats_id="1")]


def test_failed_tenant_is_retried_from_another_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_EGRESS_POOL", "http://a,http://b")
    monkeypatch.delenv("ATS_SCRAPERS_FALLBACK_PROXY", raising=False)
    monkeypatch.setattr(runner, "_egress_turn", iter(range(0, 100, 2)))
    _Scraper.calls = []

    _, _, jobs, err = asyncio.run(runner._run_scraper(_Scraper, "acme", egress=True))

    assert err is None and len(jobs) == 1
    assert _Scraper.calls == ["http://a", "http://b"]


def test_not_found_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_EGRESS_POOL", "http://a,http://b")
    _Scraper.calls = []

    _, _, _, err = asyncio.run(runner._run_scraper(_Scraper, "gone", egress=True))

    assert err == "not_found" and len(_Scraper.calls) == 1


def test_without_egress_the_proxy_is_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATS_SCRAPERS_EGRESS_POOL", "http://a,http://b")
    _Scraper.calls = []

    _, _, _, err = asyncio.run(runner._run_scraper(_Scraper, "acme"))

    assert _Scraper.calls == [None] and err and "429" in err
