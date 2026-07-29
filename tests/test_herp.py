from __future__ import annotations

import pytest

from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import HerpScraper
from ats_scrapers.scrapers.base import ScraperRegistry

LISTING_HTML = """
<html>
  <head><meta property="og:site_name" content="株式会社テスト"></head>
  <body>
    <div class="requisition-list">
      <div class="requisition-list-card">
        <a class="requisition-list-card__header-anchor" href="/v1/acme/Job-123">
          <h2>Backend Engineer</h2>
        </a>
        <div class="requisition-list-card__text">Build reliable services.</div>
        <span class="career-page-group-name-tag__text">Engineering</span>
        <span class="career-page-group-name-tag__text">Platform</span>
      </div>
      <div class="requisition-list-card">
        <a class="requisition-list-card__header-anchor" href="/v1/acme/Job-123">
          <h2>Duplicate</h2>
        </a>
      </div>
      <div class="requisition-list-card">
        <a class="requisition-list-card__header-anchor"
           href="/v1/acme/requisition-groups/group-1">
          <h2>Not a job</h2>
        </a>
      </div>
    </div>
  </body>
</html>
"""

DETAIL_HTML = """
<html><head>
  <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "Backend Engineer",
      "description": "<h2>Role</h2><p>Build reliable distributed systems.</p>",
      "datePosted": "2026-07-28T06:59:00.000Z",
      "employmentType": "正社員",
      "jobLocation": {
        "@type": "Place",
        "address": "東京都渋谷区"
      }
    }
  </script>
</head></html>
"""


def test_registry() -> None:
    assert ScraperRegistry.get(ATSType.HERP) is HerpScraper


def test_listing_parses_jobs_without_detail_requests(httpx_mock) -> None:
    httpx_mock.add_response(url="https://herp.careers/v1/acme", text=LISTING_HTML)

    jobs = HerpScraper("acme", include_descriptions=False).fetch()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Backend Engineer"
    assert job.company == "株式会社テスト"
    assert job.ats_type is ATSType.HERP
    assert job.ats_id == "Job-123"
    assert str(job.url) == "https://herp.careers/v1/acme/Job-123"
    assert str(job.apply_url) == "https://herp.careers/v1/acme/Job-123/apply"
    assert job.department == "Engineering; Platform"
    assert job.description is None


def test_default_fetch_hydrates_public_json_ld(httpx_mock) -> None:
    httpx_mock.add_response(url="https://herp.careers/v1/acme", text=LISTING_HTML)
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme/Job-123",
        text=DETAIL_HTML,
    )

    job = HerpScraper("acme").fetch()[0]

    assert job.description == ("<h2>Role</h2><p>Build reliable distributed systems.</p>")
    assert job.location == "東京都渋谷区"
    assert job.country_iso == "JP"
    assert job.region == "Asia"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "正社員"
    assert job.posted_at.isoformat() == "2026-07-28T06:59:00+00:00"


def test_detail_hydration_retries_transient_failures(httpx_mock) -> None:
    httpx_mock.add_response(url="https://herp.careers/v1/acme", text=LISTING_HTML)
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme/Job-123",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme/Job-123",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme/Job-123",
        text=DETAIL_HTML,
    )

    job = HerpScraper("acme").fetch()[0]

    assert job.description.startswith("<h2>Role</h2>")
    assert job.location == "東京都渋谷区"


def test_get_description_hydrates_missing_detail_fields(httpx_mock) -> None:
    httpx_mock.add_response(url="https://herp.careers/v1/acme", text=LISTING_HTML)
    job = HerpScraper("acme", include_descriptions=False).fetch()[0]
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme/Job-123",
        text=DETAIL_HTML,
    )

    description = HerpScraper("acme").get_description(job)

    assert description.startswith("<h2>Role</h2>")
    assert job.location == "東京都渋谷区"
    assert job.country_iso == "JP"


def test_unrecognized_html_fails_loudly(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://herp.careers/v1/acme",
        text="<html><body>maintenance</body></html>",
    )

    with pytest.raises(ScraperError, match="unrecognized careers page"):
        HerpScraper("acme").fetch()


def test_404_maps_to_company_not_found(httpx_mock) -> None:
    httpx_mock.add_response(url="https://herp.careers/v1/missing", status_code=404)

    with pytest.raises(CompanyNotFoundError):
        HerpScraper("missing").fetch()


@pytest.mark.parametrize("slug", ["https://herp.careers/v1/acme", "bad slug", "../acme"])
def test_invalid_company_id_is_rejected(slug: str) -> None:
    with pytest.raises(ScraperError, match="HerpScraper"):
        HerpScraper(slug)
