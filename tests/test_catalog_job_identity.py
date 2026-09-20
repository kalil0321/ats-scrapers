from __future__ import annotations

import pytest

import scripts.run_pipeline as runner
from ats_scrapers.models import ATSType, Job


@pytest.mark.parametrize("provider", [
    ATSType.GREENHOUSE, ATSType.LEVER, ATSType.ASHBY, ATSType.RECRUITEE,
])
def test_display_names_do_not_define_job_identity(provider: ATSType) -> None:
    first = Job(
        url="https://careers.example.com/jobs?tenant=first&job=123",
        title="Engineer",
        company="Shared Name",
        ats_type=provider,
        ats_id="123",
    )
    other_tenant = first.model_copy(update={
        "url": "https://careers.example.com/jobs?tenant=second&job=123",
    })
    renamed = first.model_copy(update={"company": "Refreshed Name"})

    assert runner._job_dedupe_key(first, {}) != runner._job_dedupe_key(other_tenant, {})
    assert runner._job_dedupe_key(first, {}) == runner._job_dedupe_key(renamed, {})
    assert runner._job_dedupe_key(first, {"dedupe_by_ats_id": True}) == (
        runner._job_dedupe_key(other_tenant, {"dedupe_by_ats_id": True})
    )
    assert runner._description_keys(first) == [
        ("url", "careers.example.com/jobs?job=123&tenant=first"),
    ]
    assert runner._description_keys(renamed) == runner._description_keys(first)
    assert runner._description_keys(other_tenant) != runner._description_keys(first)
    assert runner._row_description_keys(runner._job_to_row(first)) == (
        runner._description_keys(first)
    )


@pytest.mark.parametrize("provider", [
    ATSType.GREENHOUSE, ATSType.LEVER, ATSType.ASHBY, ATSType.RECRUITEE,
])
def test_description_cache_does_not_cross_tenants(tmp_path, provider: ATSType) -> None:
    first = Job(
        url="https://first.example.com/jobs/123",
        title="Engineer",
        company="Shared Name",
        ats_type=provider,
        ats_id="123",
    )
    other_tenant = first.model_copy(update={"url": "https://second.example.com/jobs/123"})
    cache = runner.DescriptionCache(tmp_path / "descriptions.sqlite3")
    try:
        cache.set(first, "First tenant's description")
        assert cache.get(first) == "First tenant's description"
        assert cache.get(other_tenant) is None
    finally:
        cache.close()


@pytest.mark.parametrize("provider", [
    ATSType.GREENHOUSE, ATSType.LEVER, ATSType.ASHBY, ATSType.RECRUITEE,
])
def test_job_url_variations_share_dedupe_and_cache_keys(tmp_path, provider) -> None:
    first = Job(
        url="http://CAREERS.example.com/jobs/AbC/?tenant=one&gh_jid=123&utm_source=feed",
        title="Engineer", company="Acme", ats_type=provider, ats_id="123",
    )
    variant = first.model_copy(update={
        "url": "https://careers.example.com/jobs/AbC?gh_jid=123&tenant=one#apply",
    })
    different_job = variant.model_copy(update={
        "url": "https://careers.example.com/jobs/AbC?gh_jid=456&tenant=one",
    })
    different_case = variant.model_copy(update={
        "url": "https://careers.example.com/jobs/abc?gh_jid=123&tenant=one",
    })
    assert runner._job_dedupe_key(first, {}) == runner._job_dedupe_key(variant, {})
    assert runner._description_keys(first) == runner._description_keys(variant)
    assert runner._row_description_keys(runner._job_to_row(variant)) == (
        runner._description_keys(first)
    )
    for other in (different_job, different_case):
        assert runner._description_keys(first) != runner._description_keys(other)
    cache = runner.DescriptionCache(tmp_path / "descriptions.sqlite3")
    try:
        cache.set(first, "Existing description")
        assert cache.get(variant) == "Existing description"
        assert cache.get(different_job) is None
    finally:
        cache.close()


def test_recruitee_apply_url_preserves_job_identity() -> None:
    assert runner._catalog_job_url(
        "https://acme.recruitee.com/o/engineer/apply/", "recruitee",
    ) == runner._catalog_job_url(
        "https://acme.recruitee.com/o/engineer", "recruitee",
    )
    assert runner._catalog_job_url(
        "https://acme.recruitee.com/o/apply", "recruitee",
    ) == "acme.recruitee.com/o/apply"


@pytest.mark.parametrize(("url", "expected"), [
    ("http://host:80/job", "host/job"),
    ("https://host:443/job", "host/job"),
    ("http://host:443/job", "host:443/job"),
    ("https://host:80/job", "host:80/job"),
    ("https://host:8443/job", "host:8443/job"),
    ("https://host:0/job", "host:0/job"),
    ("https://[::1]:8443/job", "[::1]:8443/job"),
    ("https://[::1:8443]/job", "[::1:8443]/job"),
])
def test_catalog_url_preserves_non_default_endpoints(url, expected) -> None:
    assert runner._catalog_job_url(url, "greenhouse") == expected
