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
    assert runner._description_keys(first) == [("url", str(first.url))]
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
