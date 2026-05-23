from __future__ import annotations

import asyncio
import csv

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

    eightfold_domain_row = {
        "name": "John Deere",
        "slug": "deere",
        "url": "https://careers.deere.com/careers",
        "domain": "johndeere.com",
    }
    kwargs = runner._eightfold_kwargs(eightfold_domain_row)
    assert kwargs["base_url"] == "https://careers.deere.com"
    assert kwargs["domain"] == "johndeere.com"
    assert kwargs["company_name"] == "John Deere"


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


def test_pipeline_reuses_previous_description_without_refetching(
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
    cached_description = "cached " + ("x" * 700)
    out_path.write_text(
        (
            "url,title,company,ats_type,ats_id,description\n"
            f"https://example.com/jobs/1,Old,Acme,custom,1,{cached_description}\n"
        ),
        encoding="utf-8",
    )

    class CachedScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            assert self.include_descriptions is True
            return [
                Job(
                    url="https://example.com/jobs/1",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="1",
                )
            ]

        def get_description(self, _job):
            raise AssertionError("cached jobs should not refetch descriptions")

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": CachedScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["description"] == cached_description


def test_pipeline_fetches_missing_description_after_cache_lookup(
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
    out_path.write_text(
        (
            "url,title,company,ats_type,ats_id,description\n"
            "https://example.com/jobs/old,Old,Acme,custom,old,previous\n"
        ),
        encoding="utf-8",
    )

    class MissingScraper:
        calls = 0

        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            assert self.include_descriptions is True
            return [
                Job(
                    url="https://example.com/jobs/2",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="2",
                )
            ]

        def get_description(self, job):
            self.__class__.calls += 1
            return f"fresh description for {job.ats_id}"

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": MissingScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["description"] == "fresh description for 2"
    assert MissingScraper.calls == 1


def test_pipeline_can_defer_scraper_descriptions_until_after_cache_lookup(
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
    cached_description = "cached " + ("x" * 700)
    out_path.write_text(
        (
            "url,title,company,ats_type,ats_id,description\n"
            f"https://example.com/jobs/1,Old,Acme,custom,1,{cached_description}\n"
        ),
        encoding="utf-8",
    )

    class DeferredScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            assert self.include_descriptions is False
            return [
                Job(
                    url="https://example.com/jobs/1",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="1",
                )
            ]

        def get_description(self, _job):
            raise AssertionError("cached jobs should not refetch descriptions")

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": DeferredScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
            "defer_descriptions_to_cache": True,
        },
    )

    rc = asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["description"] == cached_description


def test_pipeline_keeps_job_when_description_fetch_raises(
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
    out_path.write_text(
        "url,title,company,ats_type,ats_id,description\n",
        encoding="utf-8",
    )

    class RaisingDescriptionScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            return [
                Job(
                    url="https://example.com/jobs/2",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="2",
                )
            ]

        def get_description(self, _job):
            raise RuntimeError("detail API unavailable")

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": RaisingDescriptionScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["ats_id"] == "2"
    assert rows[0]["description"] == ""


def test_pipeline_writes_full_description_instead_of_preview(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_description = "d" * 700

    class FullDescriptionScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            assert self.include_descriptions is True
            return [
                Job(
                    url="https://example.com/jobs/1",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="1",
                    description=long_description,
                )
            ]

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "single",
        {
            "scraper": FullDescriptionScraper,
            "singleton": True,
            "output": "single/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("single", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader((tmp_path / "single" / "jobs.csv").open(newline="")))
    assert rows[0]["description"] == long_description


def test_description_cache_loads_previous_csv_on_disk(tmp_path) -> None:
    path = tmp_path / "jobs.csv"
    path.write_text(
        "url,title,company,ats_type,ats_id,description\n"
        "https://example.com/jobs/1,Old,Acme,custom,1,cached\n",
        encoding="utf-8",
    )
    cache = runner._load_description_cache(path)
    try:
        job = Job(
            url="https://example.com/jobs/1",
            title="Engineer",
            company="Acme",
            ats_type=ATSType.CUSTOM,
            ats_id="1",
        )

        assert cache.get(job) == "cached"
        assert cache.count == 2
    finally:
        cache.close()


def test_description_cache_count_ignores_duplicate_keys(tmp_path) -> None:
    path = tmp_path / "jobs.csv"
    path.write_text(
        "url,title,company,ats_type,ats_id,description\n"
        "https://example.com/jobs/1,Old,Acme,custom,1,cached\n",
        encoding="utf-8",
    )
    cache = runner._load_description_cache(path)
    try:
        job = Job(
            url="https://example.com/jobs/1",
            title="Engineer",
            company="Acme",
            ats_type=ATSType.CUSTOM,
            ats_id="1",
        )

        cache.set(job, "cached")

        assert cache.count == 2
    finally:
        cache.close()


def test_load_description_cache_closes_when_load_csv_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []

    class FakeCache:
        def __init__(self) -> None:
            self.closed = False
            created.append(self)

        def load_csv(self, _path) -> None:
            raise RuntimeError("bad csv")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runner, "DescriptionCache", FakeCache)

    with pytest.raises(RuntimeError, match="bad csv"):
        runner._load_description_cache(tmp_path / "jobs.csv")

    assert created[0].closed is True


def test_description_cache_unlinks_temp_file_when_init_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "cache.sqlite3"

    class FakeTempFile:
        name = str(cache_path)

        def __enter__(self):
            cache_path.touch()
            return self

        def __exit__(self, *_args):
            return False

    def fail_connect(_path):
        raise OSError("sqlite unavailable")

    monkeypatch.setattr(runner.tempfile, "NamedTemporaryFile", lambda **_kw: FakeTempFile())
    monkeypatch.setattr(runner.sqlite3, "connect", fail_connect)

    with pytest.raises(OSError):
        runner.DescriptionCache()

    assert not cache_path.exists()


def test_pipeline_closes_description_cache_on_propagating_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ats-companies").mkdir()
    (tmp_path / "ats-companies" / "fake.csv").write_text(
        "name,slug,url\nAcme,acme,https://example.com\n",
        encoding="utf-8",
    )

    class FakeCache:
        count = 0
        closed = False

        def get(self, _job):
            return None

        def set(self, _job, _description):
            pass

        def close(self):
            self.closed = True

    cache = FakeCache()

    class ExplodingScraper:
        def __init__(self, *_args, **_kwargs) -> None:
            self.include_descriptions = True

        def fetch(self):
            return [
                Job(
                    url="https://example.com/jobs/1",
                    title="Engineer",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id="1",
                    description="known",
                )
            ]

    def explode_row(_job):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_load_description_cache", lambda _path: cache)
    monkeypatch.setattr(runner, "_job_to_row", explode_row)
    monkeypatch.setitem(
        runner.CONFIGS,
        "fake",
        {
            "scraper": ExplodingScraper,
            "slug": lambda r: r["slug"],
            "csv": "ats-companies/fake.csv",
            "output": "fake/jobs.csv",
        },
    )

    with pytest.raises(OSError):
        asyncio.run(runner.run("fake", concurrency=1, max_tenants=None, timeout=1))

    assert cache.closed is True


def test_streaming_pipeline_reuses_sqlite_description_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "stream"
    out_dir.mkdir()
    out_path = out_dir / "jobs.csv"
    out_path.write_text(
        (
            "url,title,company,ats_type,ats_id,description\n"
            "https://example.com/jobs/1,Old,Acme,custom,1,cached\n"
        ),
        encoding="utf-8",
    )

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

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "stream",
        {
            "scraper": StreamingScraper,
            "singleton": True,
            "output": "stream/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("stream", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["description"] == "cached"


def test_streaming_pipeline_skips_capped_description_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "stream"
    out_dir.mkdir()
    out_path = out_dir / "jobs.csv"
    out_path.write_text(
        (
            "url,title,company,ats_type,ats_id,description\n"
            f"https://example.com/jobs/1,Old,Acme,custom,1,{'x' * 500}\n"
        ),
        encoding="utf-8",
    )

    class StreamingScraper:
        calls = 0

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

        def get_description(self, _job):
            self.__class__.calls += 1
            return "full description"

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(
        runner.CONFIGS,
        "stream",
        {
            "scraper": StreamingScraper,
            "singleton": True,
            "output": "stream/jobs.csv",
            "skip_description_cache_if_max_len_lte": 500,
        },
    )

    rc = asyncio.run(runner.run("stream", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert rows[0]["description"] == "full description"
    assert StreamingScraper.calls == 1


def test_streaming_pipeline_fetches_missing_descriptions_concurrently(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "stream"
    out_dir.mkdir()
    out_path = out_dir / "jobs.csv"

    class StreamingScraper:
        calls = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def fetch_stream(self):
            for idx in range(3):
                yield Job(
                    url=f"https://example.com/jobs/{idx}",
                    title=f"Engineer {idx}",
                    company="Acme",
                    ats_type=ATSType.CUSTOM,
                    ats_id=str(idx),
                )

        def get_description(self, job):
            self.__class__.calls += 1
            return f"streamed description {job.ats_id}"

    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(runner, "STREAM_DESCRIPTION_CONCURRENCY", 2)
    monkeypatch.setitem(
        runner.CONFIGS,
        "stream",
        {
            "scraper": StreamingScraper,
            "singleton": True,
            "output": "stream/jobs.csv",
        },
    )

    rc = asyncio.run(runner.run("stream", concurrency=1, max_tenants=None, timeout=1))

    assert rc == 0
    rows = list(csv.DictReader(out_path.open(newline="")))
    assert {row["ats_id"]: row["description"] for row in rows} == {
        "0": "streamed description 0",
        "1": "streamed description 1",
        "2": "streamed description 2",
    }
    assert StreamingScraper.calls == 3
