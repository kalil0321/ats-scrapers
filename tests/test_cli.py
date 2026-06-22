"""Tests for the `jobhive` CLI.

Argparse and command dispatch — we mock the underlying Client/scrapers so the
tests don't make network requests.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from jobhive import cli as cli_module


def test_version_prints_jobhive_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "jobhive" in out


def test_help_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "search" in out
    assert "scrape" in out
    assert "publish" in out


def test_search_command_invokes_search_with_filters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *, prefer_parquet=None):
            captured["prefer_parquet"] = prefer_parquet

        def search(self, query=None, **kwargs):
            captured.update({"query": query, **kwargs})
            return pd.DataFrame([{"title": "X", "company": "Y"}])

    monkeypatch.setattr("jobhive.client.Client", FakeClient)
    rc = cli_module.main(
        [
            "search",
            "rust",
            "--location",
            "Berlin",
            "--remote",
            "--salary-min",
            "120000",
            "--limit",
            "5",
        ]
    )
    assert rc == 0
    assert captured["query"] == "rust"
    assert captured["location"] == "Berlin"
    assert captured["remote"] is True
    assert captured["salary_min"] == 120000.0
    assert captured["limit"] == 5
    assert captured["prefer_parquet"] is None
    assert "X" in capsys.readouterr().out


def test_search_command_can_prefer_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *, prefer_parquet=None):
            captured["prefer_parquet"] = prefer_parquet

        def search(self, query=None, **_kwargs):
            return pd.DataFrame()

    monkeypatch.setattr("jobhive.client.Client", FakeClient)
    assert cli_module.main(["search", "--csv"]) == 0
    assert captured["prefer_parquet"] is False


def test_search_format_csv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def search(self, query=None, **_kwargs):
            return pd.DataFrame([{"title": "X", "company": "Y"}])

    monkeypatch.setattr("jobhive.client.Client", FakeClient)
    cli_module.main(["search", "--format", "csv"])
    out = capsys.readouterr().out
    assert "title,company" in out
    assert "X,Y" in out


def test_search_format_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def search(self, query=None, **_kwargs):
            return pd.DataFrame([{"title": "X", "company": "Y"}])

    monkeypatch.setattr("jobhive.client.Client", FakeClient)
    cli_module.main(["search", "--format", "json"])
    out = capsys.readouterr().out
    assert '"title"' in out
    assert "X" in out


def test_scrape_command_invokes_registered_scraper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jobhive.models import ATSType, Job

    sample_jobs = [
        Job(
            url="https://example.com/job/1",
            title="Backend",
            company="acme",
            ats_type=ATSType.GREENHOUSE,
            ats_id="1",
        )
    ]

    class FakeScraper:
        def __init__(self, slug: str) -> None:
            self.slug = slug

        def fetch(self):
            return sample_jobs

    monkeypatch.setattr(
        "jobhive.scrapers.get_scraper",
        lambda ats, company: FakeScraper(company),
    )
    rc = cli_module.main(["scrape", "greenhouse", "openai"])
    assert rc == 0
    assert "Backend" in capsys.readouterr().out


def test_publish_invokes_publisher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    class FakePublisher:
        def __init__(self, r2_client, *, write_parquet: bool) -> None:
            captured["write_parquet"] = write_parquet

        def publish_from_directory(self, *, source_dir, ats_csv_pattern, dated_snapshots, companies_csv):
            captured["source_dir"] = source_dir
            captured["pattern"] = ats_csv_pattern
            from types import SimpleNamespace

            return SimpleNamespace(
                manifest_key="jobhive/v1/manifest.json",
                files=["a", "b"],
                total_jobs=42,
                total_companies=7,
                duration_seconds=1.5,
            )

    class FakeR2Config:
        @staticmethod
        def from_env():
            return object()

    class FakeR2Client:
        def __init__(self, _config) -> None:
            pass

    monkeypatch.setattr("jobhive.storage.DatasetPublisher", FakePublisher)
    monkeypatch.setattr("jobhive.storage.R2Client", FakeR2Client)
    monkeypatch.setattr("jobhive.storage.R2Config", FakeR2Config)

    rc = cli_module.main(["publish", str(tmp_path), "--no-parquet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "42" in out
    assert "Manifest" in out
    assert captured["write_parquet"] is False


def test_publish_accepts_custom_pattern(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, Any] = {}

    class FakePublisher:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def publish_from_directory(self, *, ats_csv_pattern, **kwargs):
            from types import SimpleNamespace

            captured["pattern"] = ats_csv_pattern
            return SimpleNamespace(
                manifest_key="x",
                files=[],
                total_jobs=0,
                total_companies=0,
                duration_seconds=0.0,
            )

    class FakeR2Config:
        @staticmethod
        def from_env():
            return object()

    monkeypatch.setattr("jobhive.storage.DatasetPublisher", FakePublisher)
    monkeypatch.setattr("jobhive.storage.R2Client", lambda _c: None)
    monkeypatch.setattr("jobhive.storage.R2Config", FakeR2Config)

    cli_module.main(
        ["publish", str(tmp_path), "--pattern", "scrapers/{ats}/jobs.csv"]
    )
    assert captured["pattern"] == "scrapers/{ats}/jobs.csv"


def test_list_ats_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jobhive.client import Client
    from jobhive.manifest import Manifest

    sample_manifest = Manifest.model_validate(
        {
            "version": "1.0",
            "generated_at": "2026-05-03T00:00:00+00:00",
            "stats": {"total_jobs": 100, "total_companies": 5, "ats_count": 1},
            "all": {
                "csv": "https://x/all.csv",
                "rows": 100,
                "size_bytes": 100,
            },
            "by_ats": {
                "greenhouse": {
                    "csv": "https://x/gh.csv",
                    "rows": 100,
                    "size_bytes": 100,
                }
            },
            "by_date": {},
            "companies": {
                "csv": "https://x/companies.csv",
                "rows": 5,
                "size_bytes": 5,
            },
        }
    )
    fake = Client()
    fake._manifest = sample_manifest
    monkeypatch.setattr("jobhive.client._default_client", lambda: fake)

    rc = cli_module.main(["list-ats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "greenhouse" in out
    assert "100" in out


def test_unknown_command_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["nonexistent"])
    assert exc.value.code != 0


def test_jobhive_errors_are_clean_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jobhive.exceptions import ScraperError

    class FakeScraper:
        def fetch(self):
            raise ScraperError("browser required")

    monkeypatch.setattr("jobhive.scrapers.get_scraper", lambda *_args: FakeScraper())
    rc = cli_module.main(["scrape", "meta", "ignored"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip() == "jobhive: error: browser required"
    assert "Traceback" not in captured.err


def test_emit_table_format(capsys: pytest.CaptureFixture[str]) -> None:
    df = pd.DataFrame([{"title": "X", "company": "Y"}])
    cli_module._emit(df, "table")
    out = capsys.readouterr().out
    assert "title" in out
    assert "X" in out


def test_emit_csv_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    df = pd.DataFrame([{"title": "X"}])
    cli_module._emit(df, "csv")
    assert "title" in capsys.readouterr().out


def test_emit_json_writes_records(capsys: pytest.CaptureFixture[str]) -> None:
    df = pd.DataFrame([{"title": "X"}])
    cli_module._emit(df, "json")
    out = capsys.readouterr().out
    assert '"title"' in out
