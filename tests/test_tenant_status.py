from __future__ import annotations

import asyncio
import csv
import json

import pytest

import scripts.run_pipeline as runner
from ats_scrapers.exceptions import CompanyNotFoundError
from ats_scrapers.models import ATSType, Job


class _FakeScraper:
    def __init__(self, slug, **_kwargs) -> None:
        self.slug = slug

    def fetch(self):
        if self.slug == "gone":
            raise CompanyNotFoundError("board missing")
        if self.slug == "broken":
            raise RuntimeError("upstream 500")
        if self.slug == "quiet":
            return []
        return [
            Job(
                url=f"https://example.com/{self.slug}/{i}",
                title="Engineer",
                company=self.slug,
                ats_type=ATSType.CUSTOM,
                ats_id=f"{self.slug}-{i}",
            )
            for i in range(2)
        ]


@pytest.fixture
def fake_ats(tmp_path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "ats-companies").mkdir()
    (tmp_path / "ats-companies" / "fake.csv").write_text(
        "name,slug,url\n"
        "Live Co,live,https://live.example\n"
        "Gone Co,gone,https://gone.example\n"
        "Broken Co,broken,https://broken.example\n"
        "Quiet Co,quiet,https://quiet.example\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ATS_SCRAPERS_JOBS_ROOT", raising=False)
    monkeypatch.delenv("JOBHIVE_JOBS_ROOT", raising=False)
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": _FakeScraper,
            "slug": lambda row: row["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
            "skip_normalize": True,
        },
    )
    return tmp_path / "fake" / runner.TENANT_STATUS_FILENAME


def _read(path) -> dict[str, dict[str, str]]:
    with path.open(newline="") as fh:
        return {row["slug"]: row for row in csv.DictReader(fh)}


def test_records_one_status_per_tenant(fake_ats) -> None:
    asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))

    rows = _read(fake_ats)
    assert {slug: r["status"] for slug, r in rows.items()} == {
        "broken": "error",
        "gone": "not_found",
        "live": "ok",
        "quiet": "empty",
    }
    assert rows["live"]["jobs"] == "2"
    assert rows["gone"]["name"] == "Gone Co"
    assert rows["gone"]["url"] == "https://gone.example"
    assert "upstream 500" in rows["broken"]["error"]
    assert rows["gone"]["error"] == ""
    assert len({r["run_started_at"] for r in rows.values()}) == 1


def test_partial_run_keeps_previous_status_file(fake_ats) -> None:
    asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))
    before = fake_ats.read_text(encoding="utf-8")

    asyncio.run(runner.run("fake", concurrency=1, max_tenants=1, timeout=1))

    assert fake_ats.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("failure", "status", "error"),
    [
        (None, "ok", ""),
        (CompanyNotFoundError("gone"), "not_found", ""),
        (RuntimeError("feed down"), "error", "RuntimeError: feed down"),
    ],
)
def test_streaming_run_records_its_outcome(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure, status, error
) -> None:
    class StreamingScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def fetch_stream(self):
            yield Job(
                url="https://example.com/jobs/1",
                title="Engineer",
                company="Acme",
                ats_type=ATSType.CUSTOM,
                ats_id="1",
            )
            if failure is not None:
                raise failure

    monkeypatch.delenv("ATS_SCRAPERS_JOBS_ROOT", raising=False)
    monkeypatch.delenv("JOBHIVE_JOBS_ROOT", raising=False)
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "stream",
        {
            "scraper": StreamingScraper,
            "singleton": True,
            "output": "stream/jobs.csv",
            "skip_description_enrichment": True,
            "skip_normalize": True,
        },
    )

    asyncio.run(runner.run("stream", concurrency=1, max_tenants=None, timeout=1))

    rows = _read(tmp_path / "stream" / runner.TENANT_STATUS_FILENAME)
    assert rows["stream"]["status"] == status
    assert rows["stream"]["jobs"] == "1"
    assert rows["stream"]["error"] == error


def _write_previous(path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "url,title,company,ats_type,ats_id\n"
    body = "".join(f"https://example.com/{i},T,Acme,custom,{i}\n" for i in range(rows))
    path.write_text(header + body, encoding="utf-8")


def test_large_drop_keeps_previous_until_third_run(fake_ats, monkeypatch) -> None:
    jobs_csv = fake_ats.parent / "jobs.csv"
    _write_previous(jobs_csv, 2000)
    monkeypatch.setattr(runner, "DROP_GUARD_MIN_ROWS", 10)

    for attempt in (1, 2):
        rc = asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))
        assert rc == 1, attempt
        assert jobs_csv.read_text(encoding="utf-8").count("\n") == 2001

    rc = asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))
    assert jobs_csv.read_text(encoding="utf-8").count("\n") == 3  # header + 2 jobs
    state = json.loads((fake_ats.parent / runner.RUN_ROWS_FILENAME).read_text())
    assert state == {"rows": 2, "blocked": 0}


def test_drop_accepted_with_override(fake_ats, monkeypatch) -> None:
    jobs_csv = fake_ats.parent / "jobs.csv"
    _write_previous(jobs_csv, 2000)
    monkeypatch.setattr(runner, "DROP_GUARD_MIN_ROWS", 10)
    monkeypatch.setenv("ATS_SCRAPERS_ACCEPT_DROP", "1")

    asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))

    assert jobs_csv.read_text(encoding="utf-8").count("\n") == 3


def test_small_previous_output_is_not_guarded(fake_ats) -> None:
    jobs_csv = fake_ats.parent / "jobs.csv"
    _write_previous(jobs_csv, 50)

    asyncio.run(runner.run("fake", concurrency=2, max_tenants=None, timeout=1))

    assert jobs_csv.read_text(encoding="utf-8").count("\n") == 3
