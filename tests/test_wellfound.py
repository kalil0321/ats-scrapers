"""Tests for the Wellfound scraper.

The site is gated behind Akamai — every direct fetch 403s. The
library uses Firecrawl (paid, opt-in) as the rendering backend, so
the tests focus on:

- Default behaviour without a Firecrawl key (raise with hint)
- Markdown parser (Wellfound's bullet-separated meta line is
  particularly format-fragile)
- Salary / remote / location / posted-date inference from the
  meta line
- Multi-role fan-out + dedup of the per-job IDs across roles

"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from jobhive.exceptions import ScraperError
from jobhive.models import ATSType, Job
from jobhive.scrapers import ScraperRegistry, WellfoundScraper
from jobhive.scrapers.wellfound import (
    DEFAULT_ROLE_SLUGS,
    _parse_job_window,
    _parse_relative,
    _parse_salary,
)

_FIRECRAWL_RE = re.compile(r"^https://api\.firecrawl\.dev/v1/scrape$")


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import jobhive.scrapers.wellfound as w
    monkeypatch.setattr(w, "MAX_RETRIES", 1)
    monkeypatch.setattr(w, "RETRY_BASE_DELAY", 0.0)


def _markdown_with_jobs(jobs: list[dict], company: str = "Acme") -> str:
    """Build a Wellfound-shaped markdown blob with N job cards under
    a single company header — the exact layout the live site emits.
    """
    co_url = f"https://wellfound.com/company/{company.lower().replace(' ', '-')}"
    lines = [
        f"[**{company}**]({co_url})",
        "",
        "Actively Hiring",
        "",
        "Some company description51-200 Employees",
        "",
    ]
    for j in jobs:
        title = j["title"]
        ats_id = j["id"]
        slug = j.get("slug", title.lower().replace(" ", "-"))
        salary = j.get("salary", "$100k – $150k")
        location = j.get("location", "Remote • United States")
        posted = j.get("posted", "today")
        lines.append(f"[{title}](https://wellfound.com/jobs/{ats_id}-{slug}) Full-time")
        lines.append("")
        lines.append(salary)
        lines.append("")
        lines.append(location)
        lines.append("")
        lines.append(posted)
        lines.append("")
        lines.append(f"{posted}Save")
        lines.append("")
        lines.append("Apply")
        lines.append("")
    return "\n".join(lines)


def _firecrawl_response(jobs: list[dict]) -> dict:
    return {"data": {"markdown": _markdown_with_jobs(jobs)}}


# --- registry / wiring ------------------------------------------------------


def test_registry_resolves_wellfound() -> None:
    assert ScraperRegistry.get(ATSType.WELLFOUND) is WellfoundScraper


# --- gating: no key → ScraperError ------------------------------------------


def test_raises_without_firecrawl_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Library default has no Firecrawl key — must raise with a clear
    config hint, not silently return []."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(ScraperError, match="Firecrawl"):
        WellfoundScraper("any").fetch()


def test_get_description_without_firecrawl_key_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    job = Job(
        url="https://wellfound.com/jobs/1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.WELLFOUND,
        ats_id="1",
    )

    assert WellfoundScraper("any").get_description(job) is None


def test_uses_env_var_when_no_constructor_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "env-key")
    s = WellfoundScraper("any", role_slugs=())
    assert s.firecrawl_api_key == "env-key"


def test_constructor_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "env-key")
    s = WellfoundScraper("any", role_slugs=(), firecrawl_api_key="ctor-key")
    assert s.firecrawl_api_key == "ctor-key"


# --- happy path -------------------------------------------------------------


def test_parses_jobs_from_firecrawl_markdown(httpx_mock) -> None:
    """One role, one job. End-to-end markdown parse → Job."""
    md = _markdown_with_jobs(
        [{
            "id": "4173486",
            "title": "Staff Software Engineer",
            "salary": "$290k – $370k",
            "location": "San Francisco",
            "posted": "2 days ago",
        }],
        company="Atomus",
    )
    httpx_mock.add_response(
        url=_FIRECRAWL_RE,
        json={"data": {"markdown": md}},
        is_reusable=True,
    )

    jobs = WellfoundScraper(
        "any",
        firecrawl_api_key="test-key",
        role_slugs=("software-engineer",),
    ).fetch()
    j = jobs[0]
    assert j.ats_type is ATSType.WELLFOUND
    assert j.ats_id == "4173486"
    assert j.title == "Staff Software Engineer"
    assert j.company == "Atomus"
    assert j.location == "San Francisco"
    assert j.salary_currency == "USD"
    assert j.salary_min == 290000
    assert j.salary_max == 370000
    assert j.posted_at is not None
    assert str(j.url) == "https://wellfound.com/jobs/4173486-staff-software-engineer"


def test_enriches_description_from_job_page_markdown(httpx_mock) -> None:
    listing = _markdown_with_jobs([{"id": "1001", "title": "Founding Engineer"}])
    detail = "\n".join([
        "# Founding Engineer",
        "",
        "Build the core product for startup customers.",
        "",
        "You will own backend systems.",
    ])

    def serve(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        markdown = detail if "/jobs/1001-" in url else listing
        return httpx.Response(200, json={"data": {"markdown": markdown}})

    httpx_mock.add_callback(serve, url=_FIRECRAWL_RE, is_reusable=True)

    jobs = WellfoundScraper(
        "any",
        firecrawl_api_key="test-key",
        role_slugs=(),
    ).fetch()

    assert jobs[0].description == (
        "Build the core product for startup customers.\n\n"
        "You will own backend systems."
    )


def test_detail_firecrawl_permanent_error_keeps_listing_job(httpx_mock) -> None:
    listing = _markdown_with_jobs([{"id": "1001", "title": "Founding Engineer"}])

    def serve(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        if "/jobs/1001-" in url:
            return httpx.Response(402, text='{"error":"payment_required"}')
        return httpx.Response(200, json={"data": {"markdown": listing}})

    httpx_mock.add_callback(serve, url=_FIRECRAWL_RE, is_reusable=True)

    jobs = WellfoundScraper(
        "any",
        firecrawl_api_key="test-key",
        role_slugs=(),
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].ats_id == "1001"
    assert jobs[0].description is None


# --- multi-role fan-out + dedup --------------------------------------------


def test_dedupes_jobs_present_on_multiple_role_pages(httpx_mock) -> None:
    """A startup with both software-engineer and engineering-manager
    roles shows the same job on both role pages — dedup on ats_id."""
    md = _markdown_with_jobs([{"id": "1001", "title": "Founding Eng"}])
    httpx_mock.add_response(
        url=_FIRECRAWL_RE,
        json={"data": {"markdown": md}},
        is_reusable=True,
    )
    jobs = WellfoundScraper(
        "any",
        firecrawl_api_key="test-key",
        role_slugs=("software-engineer", "engineering-manager"),
    ).fetch()
    # 1 unique id across overall /jobs + 2 role pages = still 1.
    assert len(jobs) == 1


# --- meta-line parser ------------------------------------------------------


def test_parse_job_window_full_fields() -> None:
    """Wellfound emits one field per line. The window scanner should
    bind salary / location / posted / experience by *shape* (dollar
    sign, 'Remote' prefix, etc.), not by position — different roles
    drop different fields."""
    window = (
        "$290k – $370k\n\n"
        "San Francisco\n\n"
        "7years of exp\n\n"
        "2 days ago\n\n"
        "2 days agoSave\n\nApply\n\n"
    )
    loc, remote, lo, hi, posted, exp = _parse_job_window(window)
    assert loc == "San Francisco"
    assert remote is None  # no 'In office' / 'Remote' line
    assert (lo, hi) == (290_000, 370_000)
    assert exp == 7
    assert posted is not None


def test_parse_job_window_remote_only() -> None:
    """``Remote only • United States`` → is_remote=True, location='United States'."""
    window = "$80k – $130k\n\nRemote only • United States\n\nyesterday\n\n"
    loc, remote, lo, hi, _posted, _ = _parse_job_window(window)
    assert remote is True
    assert loc == "United States"
    assert (lo, hi) == (80_000, 130_000)


def test_parse_job_window_no_salary_no_remote() -> None:
    """Cards with neither salary nor remote info — keep what's there."""
    window = "Boston\n\n3 days ago\n\n"
    loc, remote, lo, hi, posted, _ = _parse_job_window(window)
    assert loc == "Boston"
    assert remote is None
    assert (lo, hi) == (None, None)
    assert posted is not None


def test_posted_date_not_misclassified_as_location() -> None:
    """Regression for the live-run bug where 'yesterday' / '2 days ago'
    leaked into the location field."""
    window = "$100k – $150k\n\nyesterday\n\nyesterdaySave\n\nApply"
    loc, _, _, _, posted, _ = _parse_job_window(window)
    assert loc is None  # no real location in this window
    assert posted is not None


def test_parse_salary_range_and_single() -> None:
    assert _parse_salary("$75k – $125k") == (75_000, 125_000)
    assert _parse_salary("$180k - $250k") == (180_000, 250_000)
    assert _parse_salary("$120k") == (120_000, 120_000)
    assert _parse_salary("Equity only") is None
    assert _parse_salary("") is None


def test_parse_relative_time() -> None:
    """``2 days ago`` / ``today`` / ``yesterday`` all yield recent datetimes."""
    assert _parse_relative("2 days ago") is not None
    assert _parse_relative("today") is not None
    assert _parse_relative("yesterday") is not None
    # Garbage falls through.
    assert _parse_relative("unknown phrase") is None


# --- error handling ---------------------------------------------------------


def test_firecrawl_500_returns_empty_for_role_not_crash(httpx_mock) -> None:
    """A single failing role mustn't crash the whole run — soft-fail
    on transient Firecrawl errors and keep going for other roles.
    Test by stubbing a 500 response and verifying the scraper
    returns [] (no jobs from this role) rather than raising."""
    httpx_mock.add_response(
        url=_FIRECRAWL_RE,
        status_code=500,
        is_reusable=True,
    )
    jobs = WellfoundScraper(
        "any",
        firecrawl_api_key="test-key",
        role_slugs=("software-engineer",),
    ).fetch()
    assert jobs == []


def test_firecrawl_402_payment_required_raises(httpx_mock) -> None:
    """402 / 401 / 403 from Firecrawl are *permanent* failures (bad
    key, quota exhausted) — surface them as ScraperError so the
    user notices, instead of returning [] for the whole board."""
    httpx_mock.add_response(
        url=_FIRECRAWL_RE,
        status_code=402,
        text='{"error":"payment_required"}',
        is_reusable=True,
    )
    with pytest.raises(ScraperError, match="402"):
        WellfoundScraper(
            "any",
            firecrawl_api_key="test-key",
            role_slugs=("software-engineer",),
        ).fetch()


# --- defaults --------------------------------------------------------------


def test_default_role_slugs_cover_tech_and_product() -> None:
    """Sanity check on the default role list — should include the core
    tech/product roles a US startup would post."""
    assert "software-engineer" in DEFAULT_ROLE_SLUGS
    assert "product-manager" in DEFAULT_ROLE_SLUGS
    assert "designer" in DEFAULT_ROLE_SLUGS
    assert "founding-engineer" in DEFAULT_ROLE_SLUGS
    assert len(DEFAULT_ROLE_SLUGS) >= 15
