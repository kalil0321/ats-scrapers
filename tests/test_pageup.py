"""Tests for the PageUp public careers scraper."""

from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers.pageup import PageUpScraper, _normalize_tenant_path


def _listing(
    rows: list[tuple[str, str, str]],
    *,
    next_page: int | None = None,
    remaining: int | None = None,
) -> str:
    rendered = "".join(
        f"""
        <tr>
          <td><a class="job-link" href="/513/cw/en/job/{job_id}/{slug}">
            {title}
          </a></td>
          <td><span class="location">{location}</span></td>
          <td><span class="close-date">
            <time datetime="2026-08-07T13:55:00Z">7 Aug 2026</time>
          </span></td>
        </tr>
        <tr class="summary"><td colspan="3">Summary for {title}</td></tr>
        """
        for job_id, title, location in rows
        for slug in [title.lower().replace(" ", "-")]
    )
    more = (
        f"""
        <p><a class="more-link" href="/513/cw/en/listing/?page={next_page}&amp;page-items=1000">
          More Jobs <span class="count">{remaining}</span>
        </a></p>
        """
        if next_page is not None
        else ""
    )
    return f"""
    <html><body>
      <div id="search-results">
        <table><tbody id="search-results-content">{rendered}</tbody></table>
        {more}
      </div>
      <div id="recent-jobs">
        <a class="job-link" href="/513/cw/en/job/999/duplicate">Duplicate</a>
      </div>
    </body></html>
    """


def _detail(job_id: str) -> str:
    return f"""
    <html><body><div id="job"><div id="job-content">
      <h3>Platform Engineer</h3>
      <p><strong>Job No.:</strong> {job_id}</p>
      <p><strong>Location:</strong> Remote - Australia</p>
      <p><strong>Employment Type:</strong> Full-time fixed term</p>
      <p><strong>Duration:</strong> 12 months</p>
      <p><strong>Department:</strong> Engineering</p>
      <p><strong>Remuneration:</strong> AUD 100,000 - 120,000 per year</p>
      <p><strong>Open Date:</strong> July 20, 2026</p>
      <p>Build reliable systems.</p>
      <a class="back-link" href="/listing">Back</a>
      <a class="apply-link" href="https://secure.dc2.pageuppeople.com/apply/513/gateway/default.aspx?c=apply&amp;lJobID={job_id}">
        Apply now
      </a>
    </div></div></body></html>
    """


def test_fetches_all_pages_and_enriches_details(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Platform Engineer", "Melbourne"),
                ("102", "Data Analyst", "Sydney"),
            ],
            next_page=2,
            remaining=1,
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=2&page-items=1000",
        text=_listing([("103", "Product Manager", "Remote")]),
    )
    for job_id, title in (
        ("101", "platform-engineer"),
        ("102", "data-analyst"),
        ("103", "product-manager"),
    ):
        httpx_mock.add_response(
            url=f"https://careers.pageuppeople.com/513/cw/en/job/{job_id}/{title}",
            text=_detail(job_id),
        )

    jobs = PageUpScraper("513/cw/en", company_name="Monash").fetch()

    assert [job.ats_id for job in jobs] == [
        "513/cw/en:101",
        "513/cw/en:102",
        "513/cw/en:103",
    ]
    assert all(job.ats_type is ATSType.PAGEUP for job in jobs)
    assert all(job.company == "Monash" for job in jobs)
    assert all(job.language == "en" for job in jobs)
    assert jobs[0].location == "Remote - Australia"
    assert jobs[0].is_remote is True
    assert jobs[0].employment_type == "CONTRACT"
    assert jobs[0].commitment == "Full-time fixed term; 12 months"
    assert jobs[0].department == "Engineering"
    assert jobs[0].salary_summary == "AUD 100,000 - 120,000 per year"
    assert jobs[0].posted_at is not None
    assert jobs[0].posted_at.tzinfo is not None
    assert jobs[0].requisition_id == "101"
    assert jobs[0].description
    assert "Build reliable systems." in jobs[0].description
    assert "Apply now" not in jobs[0].description
    assert str(jobs[0].apply_url).startswith(
        "https://secure.dc2.pageuppeople.com/apply/513/"
    )


def test_detail_location_resets_listing_remote_status(httpx_mock) -> None:
    httpx_mock.add_response(
        url=(
            "https://careers.pageuppeople.com/513/cw/en/listing/"
            "?page=1&page-items=1000"
        ),
        text=_listing([("101", "Platform Engineer", "Remote")]),
    )
    httpx_mock.add_response(
        url=(
            "https://careers.pageuppeople.com/513/cw/en/job/"
            "101/platform-engineer"
        ),
        text=_detail("101").replace(
            "Remote - Australia",
            "Sydney, Australia",
        ),
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert jobs[0].location == "Sydney, Australia"
    assert jobs[0].is_remote is None
    assert jobs[0].raw == {
        "closes_at": "2026-08-07T13:55:00Z",
        "listing_summary": "Summary for Platform Engineer",
    }


def test_include_descriptions_false_skips_detail_requests(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing([("101", "Engineer", "Melbourne")]),
    )

    jobs = PageUpScraper(
        "513/cw/en",
        company_name="Monash",
        include_descriptions=False,
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].description is None


def test_card_layout_ignores_duplicate_apply_link(httpx_mock) -> None:
    html_text = """
    <div id="search-results">
      <div id="search-results-content">
        <div class="card">
          <p class="h1">
            <a class="job-link" href="/920/cw/en/job/498105/technical-author">
              Engineering Trainee - Technical Author
            </a>
          </p>
          <p>Work Type: <span class="work-type">Student</span></p>
          <p>Location: <span class="location">Brixworth</span></p>
          <p>Department: <span class="department">Assembly Systems</span></p>
          <a class="job-link" href="/920/cw/en/job/498105/technical-author">
            Apply
          </a>
        </div>
      </div>
      <p></p>
    </div>
    """
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/920/cw/en/listing/?page=1&page-items=1000",
        text=html_text,
    )

    jobs = PageUpScraper(
        "920/cw/en",
        include_descriptions=False,
    ).fetch()

    assert len(jobs) == 1
    assert jobs[0].ats_id == "920/cw/en:498105"
    assert jobs[0].title == "Engineering Trainee - Technical Author"
    assert jobs[0].location == "Brixworth"
    assert jobs[0].department == "Assembly Systems"
    assert jobs[0].commitment == "Student"


def test_card_layout_follows_pagination(httpx_mock) -> None:
    first_page = """
    <div id="search-results">
      <div id="search-results-content">
        <div class="card">
          <a class="job-link" href="/920/cw/en/job/101/engineer">Engineer</a>
        </div>
      </div>
      <p><a class="more-link" href="/920/cw/en/listing/?page=2&amp;page-items=1000">
        More Jobs <span class="count">1</span>
      </a></p>
    </div>
    """
    second_page = """
    <div id="search-results">
      <div id="search-results-content">
        <div class="card">
          <a class="job-link" href="/920/cw/en/job/102/designer">Designer</a>
        </div>
      </div>
    </div>
    """
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/920/cw/en/listing/?page=1&page-items=1000",
        text=first_page,
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/920/cw/en/listing/?page=2&page-items=1000",
        text=second_page,
    )

    jobs = PageUpScraper("920/cw/en", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == [
        "920/cw/en:101",
        "920/cw/en:102",
    ]


def test_generic_job_links_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text="""
        <div id="search-results">
          <div id="search-results-content">
            <a class="job-link" href="/513/cw/en/job/101/engineer">Apply</a>
          </div>
        </div>
        """,
    )

    with pytest.raises(ScraperError, match="no usable jobs"):
        PageUpScraper("513/cw/en", include_descriptions=False).fetch()


def test_closed_job_between_listing_and_detail_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("102", "Designer", "Sydney"),
            ]
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        text=_detail("101"),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/102/designer",
        status_code=410,
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:101"]


def test_job_not_found_redirect_is_dropped(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("102", "Designer", "Sydney"),
            ]
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        text=_detail("101"),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/102/designer",
        status_code=301,
        headers={
            "Location": (
                "https://careers.pageuppeople.com/513/cw/en/listing/"
                "?jobnotfound=true"
            )
        },
    )
    httpx_mock.add_response(
        url=(
            "https://careers.pageuppeople.com/513/cw/en/listing/"
            "?jobnotfound=true"
        ),
        text='<a href="/513/cw/en/listing/?jobnotfound=true">Jobs</a>',
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:101"]


def test_systemic_detail_request_failure_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing([("101", "Engineer", "Melbourne")]),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        status_code=500,
        is_reusable=True,
    )

    with pytest.raises(ScraperError, match="lost every listed job"):
        PageUpScraper("513/cw/en").fetch()


def test_detail_request_failure_drops_only_failed_job(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("102", "Designer", "Sydney"),
            ]
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        text=_detail("101"),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/102/designer",
        status_code=500,
        is_reusable=True,
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:101"]


def test_descriptionless_detail_is_dropped_without_aborting(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("102", "Designer", "Sydney"),
            ]
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        text=_detail("101"),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/102/designer",
        text="<html><body><h1>Designer</h1></body></html>",
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:101"]


@pytest.mark.parametrize(
    "invalid_apply_url",
    [
        "javascript:void(0)",
        "https://[",
        "https://evil.example/apply",
        "//evil.example/apply",
        "https://secure.dc2.pageuppeople.com:444/apply",
    ],
)
def test_invalid_apply_link_does_not_abort_detail_batch(
    httpx_mock,
    invalid_apply_url: str,
) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("102", "Designer", "Sydney"),
            ]
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/101/engineer",
        text=_detail("101"),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/job/102/designer",
        text=_detail("102").replace(
            "https://secure.dc2.pageuppeople.com/apply/513/"
            "gateway/default.aspx?c=apply&amp;lJobID=102",
            invalid_apply_url,
        ),
    )

    jobs = PageUpScraper("513/cw/en").fetch()

    assert [job.ats_id for job in jobs] == [
        "513/cw/en:101",
        "513/cw/en:102",
    ]
    assert jobs[0].apply_url is not None
    assert jobs[1].apply_url is None


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/513/cw/en/job/101/engineer",
        "//127.0.0.1/513/cw/en/job/101/engineer",
        "https://careers.pageuppeople.com/999/cw/en/job/101/engineer",
        "https://careers.pageuppeople.com:444/513/cw/en/job/101/engineer",
    ],
)
def test_rejects_unsafe_detail_urls(httpx_mock, href: str) -> None:
    html_text = _listing(
        [("101", "Engineer", "Melbourne")]
    ).replace("/513/cw/en/job/101/engineer", href)
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=html_text,
    )

    with pytest.raises(ScraperError, match="unsafe job URL"):
        PageUpScraper("513/cw/en", include_descriptions=False).fetch()


def test_rejects_unsafe_pagination_url(httpx_mock) -> None:
    html_text = _listing(
        [("101", "Engineer", "Melbourne")],
        next_page=2,
        remaining=1,
    ).replace(
        "/513/cw/en/listing/?page=2&amp;page-items=1000",
        "https://evil.example/513/cw/en/listing/?page=2",
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=html_text,
    )

    with pytest.raises(ScraperError, match="unsafe listing URL"):
        PageUpScraper("513/cw/en", include_descriptions=False).fetch()


def test_reported_total_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [("101", "Engineer", "Melbourne")],
            next_page=2,
            remaining=2,
        ),
    )
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=2&page-items=1000",
        text=_listing([("102", "Designer", "Sydney")]),
    )

    with pytest.raises(ScraperError, match="reported total"):
        PageUpScraper("513/cw/en", include_descriptions=False).fetch()


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text=_listing(
            [
                ("101", "Engineer", "Melbourne"),
                ("101", "Engineer", "Sydney"),
            ]
        ),
    )

    with pytest.raises(ScraperError, match="duplicate job id"):
        PageUpScraper("513/cw/en", include_descriptions=False).fetch()


def test_numeric_job_ids_are_namespaced_by_tenant() -> None:
    first_jobs, _, _ = PageUpScraper("513/cw/en")._parse_listing(
        _listing([("101", "Engineer", "Melbourne")])
    )
    second_jobs, _, _ = PageUpScraper("920/cw/en")._parse_listing(
        _listing([("101", "Engineer", "Melbourne")]).replace(
            "/513/cw/en/",
            "/920/cw/en/",
        )
    )

    assert first_jobs[0].ats_id == "513/cw/en:101"
    assert second_jobs[0].ats_id == "920/cw/en:101"
    assert first_jobs[0].global_id != second_jobs[0].global_id
    assert first_jobs[0].requisition_id == "101"
    assert second_jobs[0].requisition_id == "101"


def test_explicit_no_jobs_returns_empty(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text="<html><body>There are no jobs available.</body></html>",
    )

    assert PageUpScraper("513/cw/en").fetch() == []


def test_missing_results_container_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000",
        text="<html><body>Maintenance</body></html>",
    )

    with pytest.raises(ScraperError, match="omitted search results"):
        PageUpScraper("513/cw/en").fetch()


_LISTING_URL = (
    "https://careers.pageuppeople.com/513/cw/en/listing/?page=1&page-items=1000"
)
_FEED_URL = "https://careers.pageuppeople.com/513/cw/en/latest_jobs.rss"


def _feed_item(job_id: str, title: str, **fields: str) -> str:
    extra = "".join(f"<job:{tag}>{value}</job:{tag}>" for tag, value in fields.items())
    return f"""
    <item>
      <guid isPermaLink="true">https://careers.pageuppeople.com/513/cw/en/job/{job_id}</guid>
      <link>https://careers.pageuppeople.com/513/cw/en/job/{job_id}</link>
      <title>{title}</title>
      <description>Short summary of {title}.</description>
      <pubDate>Wed, 11 Nov 2026 22:00:00 Z</pubDate>
      <job:refNo>{job_id}</job:refNo>
      {extra}
    </item>
    """


def _feed(*items: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <rss xmlns:a10="http://www.w3.org/2005/Atom" version="2.0">
      <channel xmlns:job="http://pageuppeople.com/">
        <link>https://careers.pageuppeople.com/513/cw/en/listing/</link>
        {"".join(items)}
      </channel>
    </rss>
    """


def _moved_listing(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL,
        status_code=302,
        headers={"Location": "https://careers.example.com.au/"},
    )


def test_tenant_moved_to_new_career_site_reads_feed(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(
        url=_FEED_URL,
        text=_feed(
            _feed_item(
                "494021",
                "Assistant Director Financial Systems",
                location="ACT|Head Office, Canberra,SA|Adelaide",
                workType="Full Time,Full Time / Part Time",
                category="Administration,Finance,Finance",
                description="&lt;p&gt;Lead the &lt;strong&gt;finance&lt;/strong&gt; systems.&lt;/p&gt;",
                applyLink=(
                    "https://secure.dc2.pageuppeople.com/apply/513/gateway/"
                    "default.aspx?c=apply&amp;lJobID=494021"
                ),
                closingDate="Thu, 26 Nov 2026 12:55:00 GMT",
                businessLayer1="Corporate",
            ),
            _feed_item("494022", "Remote Analyst", location="Remote"),
        ),
    )

    jobs = PageUpScraper("513/cw/en", company_name="DHA").fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:494021", "513/cw/en:494022"]
    first = jobs[0]
    assert str(first.url) == "https://careers.pageuppeople.com/513/cw/en/job/494021"
    assert first.company == "DHA"
    assert first.requisition_id == "494021"
    assert first.location == "Head Office, Canberra, ACT; Adelaide, SA"
    assert first.employment_type == "FULL_TIME"
    assert first.commitment == "Full Time; Full Time / Part Time"
    assert first.department == "Administration; Finance"
    assert first.description == "Lead the\nfinance\nsystems."
    assert first.posted_at is not None
    assert first.posted_at.isoformat() == "2026-11-11T22:00:00+00:00"
    assert first.application_deadline is not None
    assert first.application_deadline.isoformat() == "2026-11-26T12:55:00+00:00"
    assert str(first.apply_url).startswith("https://secure.dc2.pageuppeople.com/apply/513/")
    assert first.raw == {
        "careers_page": "https://careers.example.com.au/",
        "listing_summary": "Short summary of Assistant Director Financial Systems.",
        "business_unit": "Corporate",
    }
    assert jobs[1].is_remote is True
    assert jobs[1].description == "Short summary of Remote Analyst."


def test_moved_tenant_with_empty_feed_returns_empty(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(url=_FEED_URL, text=_feed())

    assert PageUpScraper("513/cw/en").fetch() == []


def test_moved_tenant_with_invalid_feed_fails_closed(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(url=_FEED_URL, text="<html>challenge</html>")

    with pytest.raises(ScraperError, match="invalid job feed"):
        PageUpScraper("513/cw/en").fetch()


def test_moved_tenant_feed_rejects_foreign_job_links(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(
        url=_FEED_URL,
        text=_feed(_feed_item("1", "Engineer")).replace(
            "careers.pageuppeople.com/513/cw/en/job/1",
            "careers.pageuppeople.com/920/cw/en/job/1",
        ),
    )

    with pytest.raises(ScraperError, match="unsafe job URL"):
        PageUpScraper("513/cw/en").fetch()


def test_redirect_within_pageup_keeps_listing_flow(httpx_mock) -> None:
    httpx_mock.add_response(
        url=_LISTING_URL,
        status_code=302,
        headers={"Location": _LISTING_URL},
    )
    httpx_mock.add_response(
        url=_LISTING_URL,
        text=_listing([("101", "Engineer", "Melbourne")]),
    )

    jobs = PageUpScraper("513/cw/en", include_descriptions=False).fetch()

    assert [job.ats_id for job in jobs] == ["513/cw/en:101"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("513/cw/en", "513/cw/en"),
        (
            "https://careers.pageuppeople.com/865/cw/en-us/Listing/",
            "865/cw/en-us",
        ),
        (
            "https://careers.pageuppeople.com/mob/1078/cw/en/listing/",
            "1078/cw/en",
        ),
    ],
)
def test_normalizes_supported_tenant_paths(value: str, expected: str) -> None:
    assert _normalize_tenant_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "careers.pageuppeople.com/513/cw/en",
        "http://careers.pageuppeople.com/513/cw/en",
        "https://careers.pageuppeople.com:444/513/cw/en",
        "https://evil.example/513/cw/en",
        "513/cw",
        "abc/cw/en",
        "513/cw/en/job/123/title",
    ],
)
def test_rejects_invalid_tenant_paths(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_tenant_path(value)


def test_moved_tenant_feed_honours_include_descriptions(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(url=_FEED_URL, text=_feed(_feed_item("494022", "Analyst")))

    jobs = PageUpScraper("513/cw/en", include_descriptions=False).fetch()

    assert [job.description for job in jobs] == [None]


def test_feed_job_description_is_read_from_the_feed_later(httpx_mock) -> None:
    _moved_listing(httpx_mock)
    httpx_mock.add_response(
        url=_FEED_URL,
        text=_feed(_feed_item("494022", "Analyst", description="&lt;p&gt;Analyse data.&lt;/p&gt;")),
        is_reusable=True,
    )
    scraper = PageUpScraper("513/cw/en", include_descriptions=False)
    [job] = scraper.fetch()

    assert job.description is None
    assert scraper.get_description(job) == "Analyse data."
