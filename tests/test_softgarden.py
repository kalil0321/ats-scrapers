from __future__ import annotations

import pytest

from ats_scrapers.exceptions import ScraperError
from ats_scrapers.models import ATSType
from ats_scrapers.scrapers import SoftgardenScraper
from ats_scrapers.scrapers.base import ScraperRegistry
from ats_scrapers.scrapers.softgarden import _normalize_tenant

TENANT = "abeking"
FEED_URL = f"https://{TENANT}.career.softgarden.de/jobs.feed.json"


def _job(
    job_id: int,
    *,
    title: str = "Fertigungskoordinator (m/w/d)",
) -> dict[str, object]:
    return {
        "@type": "JobPosting",
        "title": title,
        "url": (
            f"https://{TENANT}.career.softgarden.de/jobs/{job_id}/"
            "Fertigungskoordinator-m-w-d/"
        ),
        "datePosted": "2026-07-20T16:37:48.875+02:00",
        "identifier": {
            "@type": "PropertyValue",
            "name": "ABEKING & RASMUSSEN",
            "value": job_id,
        },
        "description": "<p>Build ships with an experienced team.</p>",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {
            "@type": "Organization",
            "name": "ABEKING & RASMUSSEN",
            "url": f"https://{TENANT}.career.softgarden.de",
            "industry": "Shipbuilding",
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "Schiffbauerdamm 4",
                "postalCode": "26725",
                "addressLocality": "Emden",
                "addressRegion": "Niedersachsen",
                "addressCountry": "Deutschland",
            },
        },
    }


def _feed(rows: list[dict[str, object]], *, count: int | None = None) -> dict[str, object]:
    return {
        "@context": "https://schema.org",
        "@type": "DataFeed",
        "numberOfItems": len(rows) if count is None else count,
        "dataFeedElement": [
            {
                "@type": "DataFeedItem",
                "dateModified": "2026-07-29T06:11:31.000Z",
                "item": row,
            }
            for row in rows
        ],
    }


def test_registry_resolves_softgarden() -> None:
    assert ScraperRegistry.get(ATSType.SOFTGARDEN) is SoftgardenScraper


def test_fetches_complete_schema_org_feed(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, json=_feed([_job(60111660)]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert job.ats_type is ATSType.SOFTGARDEN
    assert job.ats_id == "60111660"
    assert job.title == "Fertigungskoordinator (m/w/d)"
    assert job.company == "ABEKING & RASMUSSEN"
    assert job.location == (
        "Schiffbauerdamm 4, 26725 Emden, Niedersachsen, Deutschland"
    )
    assert job.country_iso == "DE"
    assert job.region == "Europe"
    assert job.description == "<p>Build ships with an experienced team.</p>"
    assert job.employment_type == "FULL_TIME"
    assert job.commitment == "FULL_TIME"
    assert job.posted_at.isoformat() == "2026-07-20T16:37:48.875000+02:00"
    assert str(job.apply_url) == (
        f"https://{TENANT}.career.softgarden.de/jobs/60111660/"
        "Fertigungskoordinator-m-w-d/"
    )
    assert job.raw == {
        "feed_modified_at": "2026-07-29T06:11:31.000Z",
        "industry": "Shipbuilding",
        "organization_url": f"https://{TENANT}.career.softgarden.de",
    }


def test_listing_only_mode_omits_description(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, json=_feed([_job(1)]))

    job = SoftgardenScraper(TENANT, include_descriptions=False).fetch()[0]

    assert job.description is None
    assert job.ats_id == "1"


def test_multiple_locations_are_preserved(httpx_mock) -> None:
    row = _job(1)
    row["jobLocation"] = [
        {
            "address": {
                "addressLocality": "Berlin",
                "addressCountry": "Deutschland",
            }
        },
        {
            "address": {
                "addressLocality": "Paris",
                "addressCountry": "France",
            }
        },
    ]
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert job.location == "Berlin, Deutschland; Paris, France"
    assert job.country_iso is None
    assert job.region is None


def test_unresolved_location_clears_country_for_multi_location_job(
    httpx_mock,
) -> None:
    row = _job(1)
    row["jobLocation"] = [
        {
            "address": {
                "addressLocality": "Berlin",
                "addressCountry": "Deutschland",
            }
        },
        {
            "address": {
                "addressLocality": "Sydney",
                "addressCountry": "Australia",
            }
        },
    ]
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert job.location == "Berlin, Deutschland; Sydney, Australia"
    assert job.country_iso is None
    assert job.region is None


def test_description_prose_does_not_infer_remote_status(httpx_mock) -> None:
    row = _job(1)
    row["description"] = "<p>Remote work is not possible for this role.</p>"
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert job.is_remote is None


def test_count_mismatch_fails_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, json=_feed([_job(1)], count=2))

    with pytest.raises(ScraperError, match="expected 2 jobs"):
        SoftgardenScraper(TENANT).fetch()


def test_duplicate_job_ids_fail_closed(httpx_mock) -> None:
    httpx_mock.add_response(url=FEED_URL, json=_feed([_job(1), _job(1)]))

    with pytest.raises(ScraperError, match="duplicate job ID"):
        SoftgardenScraper(TENANT).fetch()


def test_untrusted_job_url_fails_closed(httpx_mock) -> None:
    row = _job(1)
    row["url"] = "https://127.0.0.1/jobs/1/title"
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    with pytest.raises(ScraperError, match="not publicly routable"):
        SoftgardenScraper(TENANT).fetch()


def test_trusted_vanity_feed_can_link_to_canonical_softgarden_host(
    httpx_mock,
) -> None:
    row = _job(1)
    row["url"] = "https://canonical.career.softgarden.de/jobs/1/title/"
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert str(job.url) == (
        "https://canonical.career.softgarden.de/jobs/1/title/"
    )


def test_migrated_feed_can_link_to_stable_legacy_softgarden_job(
    httpx_mock,
) -> None:
    row = _job(1)
    row["url"] = (
        "https://abeking.softgarden.io/job/1/title/"
        "?jobDbPVId=123"
    )
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert str(job.url) == "https://abeking.softgarden.io/job/1/title/"


def test_feed_can_link_to_public_employer_custom_domain(httpx_mock) -> None:
    row = _job(1)
    row["url"] = (
        "https://karriere.example.com/jobs/1/title/?tracking=123"
    )
    httpx_mock.add_response(url=FEED_URL, json=_feed([row]))

    job = SoftgardenScraper(TENANT).fetch()[0]

    assert str(job.url) == "https://karriere.example.com/jobs/1/title/"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bad_slug",
        "career.softgarden.de",
        "https://career.softgarden.de/",
        "https://evil.example/",
        "https://abeking.career.softgarden.de/?preview=true",
        "https://user@abeking.career.softgarden.de/",
        "https://abeking.career.softgarden.de:invalid/",
    ],
)
def test_rejects_untrusted_tenant_identifiers(value: str) -> None:
    with pytest.raises(ScraperError):
        SoftgardenScraper(value)


def test_normalizes_trusted_host_and_url() -> None:
    assert _normalize_tenant("ABEKING.career.softgarden.de") == TENANT
    assert (
        _normalize_tenant("https://abeking.career.softgarden.de/jobs/1/title/")
        == TENANT
    )
