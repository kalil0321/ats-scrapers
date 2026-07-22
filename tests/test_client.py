"""Tests for the Layer-1 dataset client.

We mock the manifest fetch and the snapshot download via pytest-httpx so we
exercise filtering and code paths without touching the network.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

import pandas as pd
import pytest

from ats_scrapers import client as client_module
from ats_scrapers.client import Client, list_ats, search
from ats_scrapers.exceptions import ManifestError, StorageError
from ats_scrapers.models import ATSType


@pytest.fixture
def stub_client(
    monkeypatch: pytest.MonkeyPatch,
    sample_manifest_dict: dict[str, Any],
    jobs_dataframe: pd.DataFrame,
) -> Client:
    """Return a Client whose manifest + downloads are stubbed.

    Calls to `_download` return our fixture DataFrame; manifest is preloaded
    so no HTTP happens.
    """
    from ats_scrapers.manifest import Manifest

    instance = Client(prefer_parquet=False)
    instance._manifest = Manifest.model_validate(sample_manifest_dict)

    def fake_download(self: Client, url: str) -> pd.DataFrame:
        return jobs_dataframe.copy()

    monkeypatch.setattr(Client, "_download", fake_download)
    return instance


# --- search filters ----------------------------------------------------------

def test_search_no_filters_returns_all(stub_client: Client) -> None:
    df = stub_client.search()
    assert len(df) == 4


def test_search_by_title_substring_case_insensitive(stub_client: Client) -> None:
    df = stub_client.search(query="ml")
    assert len(df) == 2
    assert set(df["title"]) == {"Senior ML Engineer", "Staff ML Researcher"}


def test_search_treats_query_as_literal_text_not_regex(stub_client: Client) -> None:
    """Regex metacharacters are matched literally (GH-182: user input
    must never reach pandas' regex engine — ReDoS + crash vector)."""
    # An invalid regex must not raise — it's just text that matches nothing.
    assert stub_client.search(query="c++").empty
    assert stub_client.search(query="(unclosed").empty
    # Alternation must not act as alternation.
    assert stub_client.search(query="ML|Sales").empty
    # A catastrophic-backtracking pattern is inert as literal text.
    assert stub_client.search(query="(a+)+$" * 5, location="(x)*y").empty
    # Same for location and company filters.
    assert stub_client.search(location=".*").empty
    assert stub_client.search(company="open.i").empty


def test_search_by_location(stub_client: Client) -> None:
    df = stub_client.search(location="Paris")
    assert len(df) == 1
    assert df.iloc[0]["company"] == "Anthropic"


def test_search_by_company_substring(stub_client: Client) -> None:
    df = stub_client.search(company="open")
    assert len(df) == 1
    assert df.iloc[0]["company"] == "OpenAI"


def test_search_remote_only(stub_client: Client) -> None:
    df = stub_client.search(remote=True)
    assert set(df["company"]) == {"Stripe", "Notion"}


def test_search_non_remote_only(stub_client: Client) -> None:
    df = stub_client.search(remote=False)
    assert set(df["company"]) == {"OpenAI", "Anthropic"}


def test_search_remote_falls_back_to_location_when_flag_is_absent(
    stub_client: Client,
) -> None:
    original_download = stub_client._download
    stub_client._download = lambda url: original_download(url).drop(columns="is_remote")  # type: ignore[method-assign]
    df = stub_client.search(remote=True)
    assert set(df["company"]) == {"Stripe", "Notion"}


def test_search_salary_min_filters_by_max_column(stub_client: Client) -> None:
    df = stub_client.search(salary_min=200_000)
    assert "Junior Backend Engineer" not in set(df["title"])


def test_search_salary_max_filters_by_min_column(stub_client: Client) -> None:
    df = stub_client.search(salary_max=150_000)
    assert "Staff ML Researcher" not in set(df["title"])


def test_search_experience_max(stub_client: Client) -> None:
    df = stub_client.search(experience_max=2)
    assert len(df) == 1
    assert df.iloc[0]["experience"] == 1


def test_search_combines_filters(stub_client: Client) -> None:
    df = stub_client.search(query="engineer", remote=True)
    assert len(df) == 2


def test_search_limit_truncates(stub_client: Client) -> None:
    df = stub_client.search(limit=2)
    assert len(df) == 2


def test_search_resets_index(stub_client: Client) -> None:
    df = stub_client.search(remote=True)
    assert list(df.index) == list(range(len(df)))


def test_search_empty_result_when_no_match(stub_client: Client) -> None:
    df = stub_client.search(query="impossible-string-xyz-987")
    assert len(df) == 0


# --- load semantics ----------------------------------------------------------

def test_load_caches_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    sample_manifest_dict: dict[str, Any],
    jobs_dataframe: pd.DataFrame,
) -> None:
    from ats_scrapers.manifest import Manifest

    instance = Client()
    instance._manifest = Manifest.model_validate(sample_manifest_dict)

    download_calls = {"count": 0}

    def fake_download(self: Client, url: str) -> pd.DataFrame:
        download_calls["count"] += 1
        return jobs_dataframe.copy()

    monkeypatch.setattr(Client, "_download", fake_download)
    instance.load()
    instance.load()
    assert download_calls["count"] == 1


def test_load_with_ats_does_not_pollute_full_cache(stub_client: Client) -> None:
    stub_client.load(ats="greenhouse")
    assert stub_client._snapshot is None


def test_load_with_unknown_date_raises(stub_client: Client) -> None:
    with pytest.raises(ManifestError):
        stub_client.load(date="2099-12-31")


def test_load_rejects_both_ats_and_date(stub_client: Client) -> None:
    with pytest.raises(ValueError):
        stub_client.load(ats="greenhouse", date="2026-05-03")


def test_load_accepts_ats_as_string_or_enum(stub_client: Client) -> None:
    df1 = stub_client.load(ats="greenhouse")
    df2 = stub_client.load(ats=ATSType.GREENHOUSE)
    pd.testing.assert_frame_equal(df1, df2)


def test_load_backfills_global_id_for_legacy_dataset(stub_client: Client) -> None:
    df = stub_client.load(ats="greenhouse")
    assert df.columns[0] == "global_id"
    assert list(df["global_id"]) == [
        "ashby:1",
        "ashby:2",
        "greenhouse:3",
        "lever:4",
    ]


def test_load_preserves_published_global_id(stub_client: Client) -> None:
    original_download = stub_client._download
    stub_client._download = lambda url: original_download(url).assign(global_id="existing")  # type: ignore[method-assign]
    df = stub_client.load(ats="greenhouse")
    assert set(df["global_id"]) == {"existing"}


def test_load_uses_uuid_for_invalid_legacy_ats_id(stub_client: Client) -> None:
    original_download = stub_client._download

    def download_with_invalid_id(url: str) -> pd.DataFrame:
        df = original_download(url)
        df.loc[0, "ats_id"] = "broken\nid"
        return df

    stub_client._download = download_with_invalid_id  # type: ignore[method-assign]
    value = stub_client.load(ats="greenhouse").iloc[0]["global_id"]
    assert str(UUID(value)) == value


# A source name the installed package's ATSType enum does NOT contain.
# The hosted manifest can list a source before this package ships a
# matching scraper; that must never break the dataset client (GH-185).
_UNKNOWN_SOURCE = "futuristic_ats_9000"


def test_manifest_validates_with_unknown_source() -> None:
    """The exact step that crashed in GH-185: validating a live manifest
    whose ``by_ats`` carries a source the local enum has never heard of."""
    from ats_scrapers.manifest import Manifest
    from ats_scrapers.models import ATSType

    assert _UNKNOWN_SOURCE not in {a.value for a in ATSType}
    manifest = Manifest.model_validate(
        {
            "version": "1.0",
            "generated_at": "2026-07-22T00:00:00+00:00",
            "stats": {"total_jobs": 1, "total_companies": 1, "ats_count": 1},
            "all": {"csv": "https://x/all.csv", "rows": 1, "size_bytes": 1},
            "by_ats": {
                _UNKNOWN_SOURCE: {"csv": "https://x/f.csv", "rows": 1, "size_bytes": 1},
            },
        }
    )
    assert _UNKNOWN_SOURCE in manifest.by_ats
    assert manifest.url_for_ats(_UNKNOWN_SOURCE, prefer_parquet=False) == "https://x/f.csv"


def test_load_accepts_manifest_source_not_in_local_enum(
    stub_client: Client,
) -> None:
    stub_client._manifest = stub_client.manifest.model_copy(
        update={
            "by_ats": {
                **stub_client.manifest.by_ats,
                _UNKNOWN_SOURCE: stub_client.manifest.by_ats["greenhouse"],
            }
        }
    )
    assert len(stub_client.load(ats=_UNKNOWN_SOURCE)) == 4


def test_search_full_snapshot_tolerates_unknown_ats_rows(stub_client: Client) -> None:
    """search() over the full snapshot must not choke on rows whose
    ats_type isn't a known enum member — the beginner's default path."""
    original = stub_client._download

    def download_with_unknown(url: str) -> pd.DataFrame:
        df = original(url)
        df.loc[0, "ats_type"] = _UNKNOWN_SOURCE
        return df

    stub_client._download = download_with_unknown  # type: ignore[method-assign]
    result = stub_client.search(query="engineer")
    assert not result.empty


def test_client_defaults_to_csv_without_parquet_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_has_parquet_engine", lambda: False)
    assert Client()._prefer_parquet is False


def test_load_date_reuses_manifest_url_picker(
    monkeypatch: pytest.MonkeyPatch,
    sample_manifest_dict: dict[str, Any],
    jobs_dataframe: pd.DataFrame,
) -> None:
    from ats_scrapers.manifest import Manifest

    sample_manifest_dict["by_date"]["2026-05-04"] = {
        "parquet": "https://example.com/2026-05-04.parquet",
        "rows": 50,
        "size_bytes": 60,
    }
    instance = Client(prefer_parquet=False)
    instance._manifest = Manifest.model_validate(sample_manifest_dict)
    seen: dict[str, str] = {}

    def fake_download(self: Client, url: str) -> pd.DataFrame:
        seen["url"] = url
        return jobs_dataframe.copy()

    monkeypatch.setattr(Client, "_download", fake_download)
    instance.load(date="2026-05-04")
    assert seen["url"] == "https://example.com/2026-05-04.parquet"


# --- _download (real httpx_mock path) ----------------------------------------

def test_download_csv_via_httpx_mock(httpx_mock) -> None:
    csv_body = b"url,title\nhttps://x.com/1,Engineer\n"
    httpx_mock.add_response(url="https://example.com/data.csv", content=csv_body)
    instance = Client()
    df = instance._download("https://example.com/data.csv")
    assert len(df) == 1
    assert df.iloc[0]["title"] == "Engineer"


def test_download_parquet_via_httpx_mock(httpx_mock) -> None:
    df_in = pd.DataFrame([{"url": "https://x.com/1", "title": "X"}])
    buffer = BytesIO()
    df_in.to_parquet(buffer, index=False)
    httpx_mock.add_response(
        url="https://example.com/data.parquet",
        content=buffer.getvalue(),
    )
    instance = Client()
    df = instance._download("https://example.com/data.parquet")
    assert len(df) == 1


def test_download_parquet_without_engine_raises_storage_error(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        url="https://example.com/data.parquet",
        content=b"not important",
    )

    def fail_read_parquet(*_args, **_kwargs):
        raise ImportError("missing parquet engine")

    monkeypatch.setattr(pd, "read_parquet", fail_read_parquet)
    instance = Client()
    with pytest.raises(StorageError, match="ats-scrapers\\[parquet\\]"):
        instance._download("https://example.com/data.parquet")


def test_download_raises_on_http_error(httpx_mock) -> None:
    httpx_mock.add_response(url="https://example.com/data.csv", status_code=500)
    instance = Client()
    with pytest.raises(StorageError):
        instance._download("https://example.com/data.csv")


# --- top-level wrappers ------------------------------------------------------

def test_module_search_uses_default_client(
    monkeypatch: pytest.MonkeyPatch,
    sample_manifest_dict: dict[str, Any],
    jobs_dataframe: pd.DataFrame,
) -> None:
    from ats_scrapers.manifest import Manifest

    fake = Client()
    fake._manifest = Manifest.model_validate(sample_manifest_dict)
    monkeypatch.setattr(Client, "_download", lambda self, url: jobs_dataframe.copy())
    monkeypatch.setattr(client_module, "_default_client", lambda: fake)

    df = search(query="ml")
    assert len(df) == 2


def test_list_ats_returns_manifest_keys(
    monkeypatch: pytest.MonkeyPatch, sample_manifest_dict: dict[str, Any]
) -> None:
    from ats_scrapers.manifest import Manifest

    fake = Client()
    fake._manifest = Manifest.model_validate(sample_manifest_dict)
    monkeypatch.setattr(client_module, "_default_client", lambda: fake)

    assert "greenhouse" in list_ats()


# --- context manager ---------------------------------------------------------

def test_client_context_manager_closes_http_client() -> None:
    with Client() as c:
        assert c._http_client is not None
    # internal flag — verifies close() ran without raising


def test_client_does_not_close_externally_provided_http_client() -> None:
    import httpx

    external = httpx.Client()
    c = Client(http_client=external)
    c.close()
    # Should still be usable after Client.close
    assert external.is_closed is False
    external.close()
