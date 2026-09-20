from __future__ import annotations

import csv
from io import BytesIO, StringIO
from uuid import UUID

import pandas as pd
import pytest

from ats_scrapers.client import Client, _normalize_dataset
from ats_scrapers.identity import URL_SCOPED_PROVIDERS, build_global_id, canonical_job_url
from ats_scrapers.models import ATSType, Job
from pipeline.publisher import DatasetPublisher, _with_global_ids
from scripts.run_pipeline import JOB_CSV_FIELDS, _job_to_row


@pytest.mark.parametrize("provider", sorted(URL_SCOPED_PROVIDERS))
def test_versioned_id_has_fixed_encoding(provider) -> None:
    assert build_global_id(provider, "001", "https://jobs.example.com/acme/001") == (
        f"{provider}:v2:e20dade34c09c6b4d41a06641c6b041412c778959be86d2cf2fd8f5ecb1d2f4f"
    )


@pytest.mark.parametrize("provider", sorted(URL_SCOPED_PROVIDERS))
def test_tenants_are_distinct_but_display_names_are_not_identity(provider) -> None:
    payload = {
        "url": "https://jobs.example.com/acme/001", "company": "Shared Name",
        "title": "Engineer", "ats_type": provider, "ats_id": "001",
    }
    first = Job(**payload)
    renamed = Job(**(payload | {"company": "Fresh Name", "title": "Updated title"}))
    other_tenant = Job(**(payload | {"url": "https://jobs.example.com/other/001"}))
    other_job = Job(**(payload | {"ats_id": "002"}))
    assert first.global_id == renamed.global_id
    assert len({first.global_id, other_tenant.global_id, other_job.global_id}) == 3
    assert first.ats_id == "001"
    assert Job.model_validate(first.model_dump(mode="json")).global_id == first.global_id


@pytest.mark.parametrize(("provider", "first", "second"), [
    ("ashby", "http://CAREERS.example.com:80/AbC/?tenant=acme&job=001&utm_source=feed",
     "https://careers.example.com:443/AbC?job=001&tenant=acme#apply"),
    ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&gh_src=foo",
     "https://boards.greenhouse.io/acme/jobs/123"),
    ("lever", "https://jobs.lever.co/acme/123?lever-origin=applied&lever-source=feed",
     "https://jobs.lever.co/acme/123"),
    ("recruitee", "https://acme.recruitee.com/o/engineer/apply/",
     "https://acme.recruitee.com/o/engineer"),
    ("ashby", "https://example.com/caf%C3%A9", "https://example.com/café"),
])
def test_presentation_variants_have_the_same_id(provider, first, second) -> None:
    assert build_global_id(provider, "001", first) == build_global_id(provider, "001", second)


@pytest.mark.parametrize(("first", "second"), [
    ("https://example.com/jobs?tenant=first&id=1", "https://example.com/jobs?tenant=second&id=1"),
    ("https://example.com/jobs?gh_jid=1", "https://example.com/jobs?gh_jid=2"),
    ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1?gh_jid=2"),
    ("https://example.com/AbC", "https://example.com/abc"),
    ("https://example.com/job", "https://example.com:80/job"),
    ("https://example.com/job", "http://example.com:443/job"),
    ("https://example.com/job", "https://example.com:0/job"),
    ("https://example.com/job", "https://other.example.com/job"),
    ("https://example.com/jobs?tenant=a&tenant=b", "https://example.com/jobs?tenant=b&tenant=a"),
])
def test_meaningful_url_components_do_not_collapse(first, second) -> None:
    assert build_global_id("greenhouse", "1", first) != build_global_id("greenhouse", "1", second)


def test_ipv6_and_literal_apply_slug() -> None:
    assert canonical_job_url("https://[::1]:8443/job", "ashby") == "[::1]:8443/job"
    assert canonical_job_url("https://acme.recruitee.com/o/apply", "recruitee") == "acme.recruitee.com/o/apply"


def test_provider_specific_tracking_is_not_removed_from_other_sources() -> None:
    assert canonical_job_url("https://example.com/job?gh_src=tenant", "ashby").endswith("?gh_src=tenant")


@pytest.mark.parametrize("provider", sorted(URL_SCOPED_PROVIDERS))
@pytest.mark.parametrize("url", ["", "not-a-url", "https://[", "https://example.com:bad/job"])
def test_missing_or_invalid_url_never_creates_unscoped_id(provider, url) -> None:
    value = build_global_id(provider, "123", url)
    assert UUID(value).version == 4


@pytest.mark.parametrize("bad_id", [None, "", "   ", "x\n1", "x\x1b1", "x\x7f1"])
def test_invalid_ids_follow_same_model_and_client_policy(bad_id) -> None:
    payload = {"ats_type": "ashby", "ats_id": bad_id, "url": "https://example.com/job"}
    job = Job(**payload, title="Engineer", company="Acme")
    dataset = _normalize_dataset(pd.DataFrame([payload]))
    assert UUID(job.global_id).version == 4
    assert UUID(dataset.iloc[0]["global_id"]).version == 4


def test_other_sources_keep_their_existing_composite_ids() -> None:
    for provider in ATSType:
        if provider.value not in URL_SCOPED_PROVIDERS:
            assert build_global_id(provider.value, "tenant:001", "https://example.com/job") == (
                f"{provider.value}:tenant:001"
            )


def test_mixed_client_snapshot_fills_only_absent_ids() -> None:
    frame = pd.DataFrame({
        "ats_type": ["ashby"] * 4, "ats_id": ["001"] * 4,
        "url": ["https://jobs.example.com/acme/001"] * 4,
        "global_id": ["ashby:001", None, "", "  "],
    }, index=[5, 5, 7, 8])
    result = _normalize_dataset(frame)
    assert result is frame
    assert result.iloc[0]["global_id"] == "ashby:001"
    expected = build_global_id("ashby", "001", "https://jobs.example.com/acme/001")
    assert result["global_id"].tolist() == ["ashby:001", expected, expected, expected]


def test_empty_legacy_snapshot_has_global_id_column() -> None:
    frame = pd.DataFrame(columns=["ats_type", "ats_id", "url"])
    result = _normalize_dataset(frame)
    assert result.empty
    assert result.columns[0] == "global_id"


def test_client_download_preserves_exact_text_ids(httpx_mock) -> None:
    url = "https://example.com/jobs.csv"
    ids = ["001", "12345678901234567890123456", "NA", "null"]
    csv_data = "ats_type,ats_id,url\n" + "".join(
        f"ashby,{native_id},https://example.com/job/{native_id}\n" for native_id in ids
    )
    httpx_mock.add_response(url=url, text=csv_data)
    with Client() as client:
        frame = _normalize_dataset(client._download(url))
    assert frame["ats_id"].tolist() == ids
    assert frame["global_id"].tolist() == [
        build_global_id("ashby", native_id, f"https://example.com/job/{native_id}") for native_id in ids
    ]


def test_client_preserves_nullable_integer_ids_from_legacy_parquet(httpx_mock) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    buffer = BytesIO()
    pq.write_table(pa.table({
        "ats_type": ["ashby", "ashby"],
        "ats_id": pa.array([9007199254740993, None], type=pa.int64()),
        "url": ["https://example.com/1", "https://example.com/2"],
    }), buffer)
    url = "https://example.com/jobs.parquet"
    httpx_mock.add_response(url=url, content=buffer.getvalue())
    with Client() as client:
        frame = _normalize_dataset(client._download(url))
    assert frame.iloc[0]["ats_id"] == 9007199254740993
    assert frame.iloc[0]["global_id"] == build_global_id("ashby", "9007199254740993", "https://example.com/1")
    assert UUID(frame.iloc[1]["global_id"]).version == 4


@pytest.mark.parametrize("include_ids", [True, False])
def test_model_runner_publisher_and_client_agree(tmp_path, fake_r2, include_ids) -> None:
    expected = {}
    for provider in sorted(URL_SCOPED_PROVIDERS):
        directory = tmp_path / provider
        directory.mkdir()
        fields = JOB_CSV_FIELDS if include_ids else [field for field in JOB_CSV_FIELDS if field != "global_id"]
        with (directory / "jobs.csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for index, native_id in enumerate(["001", "12345678901234567890123456", "NA"]):
                job = Job(
                    url=f"https://{provider}.example.com/acme/{native_id}",
                    title=f"Engineer {index}", company=f"{provider} employer",
                    ats_type=provider, ats_id=native_id,
                )
                expected[str(job.url)] = (native_id, job.global_id)
                row = _job_to_row(job)
                assert row["global_id"] == job.global_id
                if not include_ids:
                    row.pop("global_id")
                writer.writerow(row)

    result = DatasetPublisher(fake_r2).publish_from_directory(tmp_path)
    assert result.total_jobs == 12
    for key, upload in fake_r2.uploads.items():
        if key.endswith(".csv"):
            frame = pd.read_csv(BytesIO(upload["data"]), converters={"ats_id": str, "global_id": str})
        elif key.endswith(".parquet"):
            frame = pd.read_parquet(BytesIO(upload["data"]))
        else:
            continue
        assert frame["global_id"].notna().all()
        assert frame["global_id"].is_unique
        assert _normalize_dataset(frame) is frame
        for row in frame.to_dict("records"):
            assert (row["ats_id"], row["global_id"]) == expected[row["url"]]


def test_publisher_backfill_preserves_published_ids_and_uuid_consistency(tmp_path, fake_r2) -> None:
    import polars as pl

    source = pl.DataFrame({
        "ats_type": ["ashby"] * 3,
        "ats_id": ["001", "002", None],
        "url": ["https://example.com/1", "https://example.com/2", "https://example.com/3"],
        "global_id": ["historical-id", " ", None],
    })
    actual = _with_global_ids(source.lazy()).collect()["global_id"].to_list()
    assert actual[0] == "historical-id"
    assert actual[1] == build_global_id("ashby", "002", "https://example.com/2")
    assert UUID(actual[2]).version == 4

    directory = tmp_path / "ashby"
    directory.mkdir()
    (directory / "jobs.csv").write_text(
        "url,title,company,ats_id\nhttps://example.com/1,Engineer,Acme,\n"
    )
    DatasetPublisher(fake_r2).publish_from_directory(tmp_path)
    csv_row = next(csv.DictReader(StringIO(fake_r2.uploads["jobhive/v1/ashby/jobs.csv"]["data"].decode())))
    global_frame = pd.read_parquet(BytesIO(fake_r2.uploads["jobhive/v1/all.parquet"]["data"]))
    assert UUID(csv_row["global_id"]).version == 4
    assert global_frame.iloc[0]["global_id"] == csv_row["global_id"]
