"""Tests for the Jobvite public careers scraper."""

from __future__ import annotations

import json

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType, Job
from ats_scrapers.scrapers.jobvite import (
    JobviteScraper,
    _apply_detail,
    _normalize_tenant_path,
)


def _listing(
    rows: list[tuple[str, str, str]],
    *,
    start: int,
    end: int,
    total: int,
) -> str:
    items = "".join(
        f"""
        <li class="mb1">
          <a class="jv-job-item flex-row-md flex-col" href="/acme/job/{job_id}">
            <div class="jv-job-list-name"><p>{title}</p></div>
            <div class="jv-job-list-location"><p>{location}</p></div>
          </a>
        </li>
        """
        for job_id, title, location in rows
    )
    return f"""
    <html><body>
      <div class="jv-job-list"><ul>{items}</ul></div>
      <div class="jv-pagination">
        <div class="jv-pagination-text">{start}-{end} of {total}</div>
      </div>
    </body></html>
    """


def _detail(
    job_id: str,
    *,
    title: str = "Engineer",
    employment_type: str = "FULL_TIME",
    locations: list[dict[str, object]] | None = None,
    job_location_type: str | None = None,
) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "datePosted": "2026-07-20",
        "description": "<p>Build reliable systems.</p><p>Ship carefully.</p>",
        "employmentType": employment_type,
        "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
        "identifier": job_id,
        "industry": "Engineering",
        "jobLocation": locations
        or [
            {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Paris",
                    "addressCountry": "France",
                },
            }
        ],
        "title": title,
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "EUR",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 90000,
                "maxValue": 120000,
                "unitText": "YEAR",
            },
        },
    }
    if job_location_type is not None:
        posting["jobLocationType"] = job_location_type
    return f"""
    <html><body>
      <h2 class="jv-header">{title}</h2>
      <p class="jv-job-detail-meta">
        Engineering <span class="jv-inline-separator">|</span> Paris, France
      </p>
      <a class="jv-button jv-button-apply" href="/acme/job/{job_id}/apply">Apply</a>
      <div class="jv-job-detail-description"><h3>Description</h3>
        <p>Build reliable systems.</p>
      </div>
      <script type="application/ld+json">{json.dumps(posting)}</script>
    </body></html>
    """


def test_fetches_all_pages_and_enriches_details(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [
                ("a1", "Platform Engineer", "Paris, France"),
                ("a2", "Product Manager", "2 Locations"),
            ],
            start=1,
            end=2,
            total=3,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=1",
        text=_listing(
            [("a3", "Data Analyst", "Remote")],
            start=3,
            end=3,
            total=3,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        text=_detail("a1", title="Platform Engineer"),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a2",
        text=_detail(
            "a2",
            title="Product Manager",
            locations=[
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Paris",
                        "addressCountry": "France",
                    },
                },
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Berlin",
                        "addressCountry": "Germany",
                    },
                },
            ],
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a3",
        text=_detail("a3", title="Data Analyst", employment_type="CONTRACT"),
    )

    jobs = JobviteScraper("acme", company_name="Acme").fetch()

    assert [job.ats_id for job in jobs] == ["a1", "a2", "a3"]
    assert all(job.ats_type is ATSType.JOBVITE for job in jobs)
    assert jobs[0].company == "Acme Corp"
    assert jobs[0].description == "Build reliable systems.\nShip carefully."
    assert jobs[0].department == "Engineering"
    assert jobs[0].employment_type == "FULL_TIME"
    assert jobs[0].posted_at is not None
    assert jobs[0].posted_at.tzinfo is not None
    assert jobs[0].salary_min == 90000
    assert jobs[0].salary_max == 120000
    assert jobs[0].salary_currency == "EUR"
    assert jobs[0].salary_period == "YEAR"
    assert str(jobs[0].apply_url) == "https://jobs.jobvite.com/acme/job/a1/apply"
    assert jobs[1].location == "Paris, France; Berlin, Germany"
    assert jobs[2].is_remote is True
    assert jobs[2].employment_type == "CONTRACT"


def test_include_descriptions_false_skips_detail_requests(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris, France")],
            start=1,
            end=1,
            total=1,
        ),
    )

    jobs = JobviteScraper(
        "acme",
        company_name="Acme",
        include_descriptions=False,
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].description is None


def test_closed_job_between_listing_and_detail_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [
                ("a1", "Engineer", "Paris"),
                ("a2", "Designer", "London"),
            ],
            start=1,
            end=2,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        text=_detail("a1"),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a2",
        status_code=404,
    )

    jobs = JobviteScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["a1"]


def test_descriptionless_detail_is_dropped_without_aborting(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [
                ("a1", "Engineer", "Paris"),
                ("a2", "Designer", "London"),
            ],
            start=1,
            end=2,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        text=_detail("a1"),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a2",
        text="<html><body><h1>Designer</h1></body></html>",
    )

    jobs = JobviteScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["a1"]


def test_systemic_detail_request_failure_preserves_listing_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=1,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        status_code=500,
        is_reusable=True,
    )

    jobs = JobviteScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["a1"]
    assert jobs[0].description is None


def test_detail_request_failure_preserves_listing_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [
                ("a1", "Engineer", "Paris"),
                ("a2", "Designer", "London"),
            ],
            start=1,
            end=2,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        text=_detail("a1"),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a2",
        status_code=500,
        is_reusable=True,
    )

    jobs = JobviteScraper("acme").fetch()

    assert [job.ats_id for job in jobs] == ["a1", "a2"]
    assert jobs[0].description is not None
    assert jobs[1].description is None


def test_table_listing_layout_reads_title_and_location(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text="""
        <div class="jv-job-list">
          <table><tr>
            <td class="jv-job-list-name">
              <a href="/acme/job/a1">Platform Engineer</a>
            </td>
            <td class="jv-job-list-location">Paris, France</td>
          </tr></table>
        </div>
        <div class="jv-pagination-text">1-1 of 1</div>
        """,
    )

    jobs = JobviteScraper("acme", include_descriptions=False).fetch()

    assert jobs[0].title == "Platform Engineer"
    assert jobs[0].location == "Paris, France"


def test_careers_tenant_accepts_canonical_detail_path(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/careers/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=1,
        ),
    )

    jobs = JobviteScraper(
        "careers/acme",
        include_descriptions=False,
    ).fetch()

    assert str(jobs[0].url) == "https://jobs.jobvite.com/acme/job/a1"


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/acme/job/a1",
        "//127.0.0.1/acme/job/a1",
        "https://jobs.jobvite.com/other/job/a1",
    ],
)
def test_rejects_unsafe_detail_urls(httpx_mock, href: str) -> None:
    html_text = _listing(
        [("a1", "Engineer", "Paris")],
        start=1,
        end=1,
        total=1,
    ).replace("/acme/job/a1", href)
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=html_text,
    )

    with pytest.raises(ScraperError, match="unsafe job URL"):
        JobviteScraper("acme", include_descriptions=False).fetch()


@pytest.mark.parametrize(
    "href",
    [
        "https://jobs.jobvite.com:444/acme/job/a1",
        "https://user@jobs.jobvite.com/acme/job/a1",
    ],
)
def test_rejects_unsafe_detail_authorities(httpx_mock, href: str) -> None:
    html_text = _listing(
        [("a1", "Engineer", "Paris")],
        start=1,
        end=1,
        total=1,
    ).replace("/acme/job/a1", href)
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=html_text,
    )

    with pytest.raises(ScraperError, match="unsafe job URL"):
        JobviteScraper("acme", include_descriptions=False).fetch()


def test_explicit_no_openings_returns_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text='<div class="jv-job-list">No jobs are currently available.</div>',
    )

    assert JobviteScraper("acme").fetch() == []


def test_missing_job_list_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text="<html><body>Maintenance</body></html>",
    )

    with pytest.raises(ScraperError, match="job-list container"):
        JobviteScraper("acme").fetch()


def test_listing_supports_legacy_anchor_classes(httpx_mock) -> None:
    html_text = _listing(
        [("a1", "Engineer", "Paris")],
        start=1,
        end=1,
        total=1,
    ).replace("jv-job-item flex-row-md flex-col", "flex-row")
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=html_text,
    )

    jobs = JobviteScraper("acme", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["a1"]


def test_missing_pagination_fails_closed(httpx_mock) -> None:
    html_text = _listing(
        [("a1", "Engineer", "Paris")],
        start=1,
        end=1,
        total=1,
    ).replace('<div class="jv-pagination-text">1-1 of 1</div>', "")
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=html_text,
    )

    with pytest.raises(ScraperError, match="pagination metadata"):
        JobviteScraper("acme").fetch()


def test_pagination_gap_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=3,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=1",
        text=_listing(
            [("a2", "Designer", "London")],
            start=3,
            end=3,
            total=3,
        ),
    )

    with pytest.raises(ScraperError, match="pagination gap"):
        JobviteScraper("acme").fetch()


def test_total_change_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=1",
        text=_listing(
            [("a2", "Designer", "London")],
            start=2,
            end=2,
            total=3,
        ),
    )

    with pytest.raises(ScraperError, match="total changed"):
        JobviteScraper("acme").fetch()


def test_catalogue_becoming_empty_mid_pagination_fails_closed(
    httpx_mock,
) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=1",
        text='<div class="jv-job-list">No jobs are currently available.</div>',
    )

    with pytest.raises(ScraperError, match="became empty"):
        JobviteScraper("acme").fetch()


def test_duplicate_job_id_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=2,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=1",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=2,
            end=2,
            total=2,
        ),
    )

    with pytest.raises(ScraperError, match="duplicate job id"):
        JobviteScraper("acme").fetch()


def test_all_details_without_descriptions_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/search?p=0",
        text=_listing(
            [("a1", "Engineer", "Paris")],
            start=1,
            end=1,
            total=1,
        ),
    )
    httpx_mock.add_response(
        url="https://jobs.jobvite.com/acme/job/a1",
        text="<html><body><h2 class='jv-header'>Engineer</h2></body></html>",
    )

    with pytest.raises(ScraperError, match="lost every listed job"):
        JobviteScraper("acme").fetch()


def test_detail_dom_fallback_populates_description() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
    )
    _apply_detail(
        job,
        """
        <p class="jv-job-detail-meta">
          Engineering <span class="jv-inline-separator">|</span> Paris, France
        </p>
        <div class="jv-job-detail-description">
          <h3>Description</h3><p>Build systems.</p><p>Help customers.</p>
        </div>
        """,
    )

    assert job.description == "Build systems.\nHelp customers."
    assert job.department == "Engineering"
    assert job.location == "Paris, France"


def test_jsonld_entities_are_decoded_after_json_parsing() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
    )
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "description": "<p>Use &quot;safe&quot; APIs.</p>",
    }

    _apply_detail(
        job,
        (
            '<script type="application/ld+json">'
            f"{json.dumps(posting)}"
            "</script>"
        ),
    )

    assert job.description == 'Use "safe" APIs.'


def test_structured_remote_location_updates_remote_status() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
        location="2 Locations",
    )

    _apply_detail(
        job,
        _detail(
            "a1",
            locations=[
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Remote",
                        "addressCountry": "France",
                    },
                }
            ],
        ),
    )

    assert job.location == "Remote, France"
    assert job.is_remote is True


def test_structured_telecommute_marker_updates_remote_status() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
        location="Paris, France",
    )

    _apply_detail(
        job,
        _detail("a1", job_location_type="TELECOMMUTE"),
    )

    assert job.location == "Paris, France"
    assert job.is_remote is True


def test_telecommute_survives_generic_location_replacement() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
        location="2 Locations",
    )

    _apply_detail(
        job,
        _detail("a1", job_location_type="TELECOMMUTE"),
    )

    assert job.location == "Paris, France"
    assert job.is_remote is True


def test_telecommute_survives_detail_meta_location_replacement() -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
        location="2 Locations",
    )
    html_text = _detail(
        "a1",
        locations=[],
        job_location_type="TELECOMMUTE",
    ).replace(
        "</body>",
        '<div class="jv-job-detail-meta">Engineering | Paris, France</div>'
        "</body>",
    )

    _apply_detail(job, html_text)

    assert job.location == "Paris, France"
    assert job.is_remote is True


@pytest.mark.parametrize(
    "href",
    [
        "javascript:void(0)",
        "https://[",
        "https://evil.example/apply",
        "https://jobs.jobvite.com:444/acme/job/a1/apply",
        "https://user@jobs.jobvite.com/acme/job/a1/apply",
    ],
)
def test_invalid_apply_link_is_ignored(href: str) -> None:
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
    )
    html_text = _detail("a1").replace(
        "/acme/job/a1/apply",
        href,
    )

    _apply_detail(job, html_text)

    assert job.apply_url is None


def test_get_description_uses_detail_page(httpx_mock) -> None:
    scraper = JobviteScraper("acme")
    job = Job(
        url="https://jobs.jobvite.com/acme/job/a1",
        title="Engineer",
        company="Acme",
        ats_type=ATSType.JOBVITE,
        ats_id="a1",
    )
    httpx_mock.add_response(
        url=str(job.url),
        text=_detail("a1"),
    )

    assert scraper.get_description(job) == (
        "Build reliable systems.\nShip carefully."
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://jobs.jobvite.com/acme",
        "https://example.com/acme",
        "https://jobs.jobvite.com:444/acme",
        "https://user@jobs.jobvite.com/acme",
        "careers",
        "careers/acme/extra",
        "../acme",
        "acme?x=1",
    ],
)
def test_invalid_tenant_paths_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_tenant_path(value)


def test_full_jobvite_url_normalizes_to_tenant_path() -> None:
    assert (
        _normalize_tenant_path(
            "https://jobs.jobvite.com/careers/acme/search"
        )
        == "careers/acme"
    )


def test_registry_contains_jobvite() -> None:
    from ats_scrapers.scrapers.base import ScraperRegistry

    assert ScraperRegistry.get(ATSType.JOBVITE) is JobviteScraper
