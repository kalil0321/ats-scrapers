from __future__ import annotations

import asyncio

import pytest

import scripts.run_pipeline as runner
from jobhive.models import ATSType, Job


def test_provider_slug_normalizers_match_current_company_csv_shape() -> None:
    sf_row = {
        "name": "Ace1950",
        "slug": "ace1950",
        "url": "https://ace1950.jobs2web.com",
    }
    assert runner._successfactors_slug(sf_row) == "https://ace1950.jobs2web.com"

    oracle_row = {
        "name": "ABM US",
        "slug": "eiqg",
        "url": (
            "https://eiqg.fa.us2.oraclecloud.com/"
            "hcmUI/CandidateExperience/en/sites/CX_1"
        ),
    }
    assert (
        runner._oracle_slug(oracle_row)
        == "https://eiqg.fa.us2.oraclecloud.com?site_number=CX_1"
    )

    eightfold_row = {
        "name": "Amdocs",
        "slug": "amdocs",
        "url": "https://amdocs.eightfold.ai/careers",
    }
    assert runner._eightfold_kwargs(eightfold_row)["base_url"] == (
        "https://amdocs.eightfold.ai"
    )


def test_catastrophic_failure_preserves_previous_jobs_csv(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ats-companies").mkdir()
    (tmp_path / "ats-companies" / "fake.csv").write_text(
        "name,slug,url\nAcme,acme,https://example.com\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "fake"
    out_dir.mkdir()
    out_path = out_dir / "jobs.csv"
    previous = "url,title,company,ats_type,ats_id\nhttps://old,Old,Acme,custom,1\n"
    out_path.write_text(previous, encoding="utf-8")

    class FailingScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def fetch(self):
            raise RuntimeError("down")

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": FailingScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 1
    assert out_path.read_text(encoding="utf-8") == previous
    assert not (out_dir / ".jobs.csv.tmp").exists()


def test_singleton_success_returns_zero_exit_code(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SingletonScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def fetch(self):
            return [
                Job(
                    url="https://example.com/job/1",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="1",
                )
            ]

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "single",
        {
            "scraper": SingletonScraper,
            "singleton": True,
            "output": "single/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("single", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    assert (tmp_path / "single" / "jobs.csv").exists()
