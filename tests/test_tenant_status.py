from __future__ import annotations

import asyncio
import csv

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
