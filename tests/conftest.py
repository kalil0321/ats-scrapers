"""Shared fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


@dataclass
class FakeR2:
    """Drop-in stand-in for `R2Client`.

    Captures uploads in memory so tests can assert on keys, payloads, and
    headers without hitting the network. Also supports list/delete so we
    can exercise the pruning logic.
    """

    bucket: str = "test-bucket"
    public_base: str | None = "https://cdn.example.com"
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)

    def upload_bytes(
        self,
        data: bytes,
        key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        self.uploads[key] = {
            "data": data,
            "content_type": content_type,
            "cache_control": cache_control,
        }
        return key

    def upload(
        self,
        local_path: str | Path,
        key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> str:
        path = Path(local_path)
        return self.upload_bytes(
            path.read_bytes(),
            key,
            content_type=content_type,
            cache_control=cache_control,
        )

    def list(self, prefix: str = ""):
        for key in self.uploads:
            if key.startswith(prefix):
                yield {"Key": key, "Size": len(self.uploads[key]["data"])}

    def get_bytes(self, key: str) -> bytes | None:
        entry = self.uploads.get(key)
        return entry["data"] if entry else None

    def delete_many(self, keys: list[str]) -> int:
        for k in keys:
            self.uploads.pop(k, None)
            self.deleted.append(k)
        return len(keys)

    def public_url(self, key: str) -> str | None:
        if not self.public_base:
            return None
        return f"{self.public_base}/{key}"


@pytest.fixture
def fake_r2() -> FakeR2:
    return FakeR2()


@pytest.fixture
def fake_r2_no_public() -> FakeR2:
    return FakeR2(public_base=None)


@pytest.fixture
def ats_csv_dir(tmp_path: Path) -> Path:
    """Layout mimicking stapply-ai/data: <ats>/jobs.csv per platform.

    Companies are distinct per ATS (``{ats}-co{i}``) so the cross-ATS dedup
    in the publisher is a no-op for this fixture — exercising baseline
    behavior. See ``ats_csv_dir_with_duplicates`` for the dedup case.
    """
    for ats in ("greenhouse", "lever", "ashby"):
        ats_dir = tmp_path / ats
        ats_dir.mkdir()
        df = pd.DataFrame(
            [
                {
                    "url": f"https://{ats}.com/job/{i}",
                    "title": f"Engineer {i}",
                    "location": "Remote" if i % 2 else "Paris",
                    "company": f"{ats}-co{i}",
                    "ats_id": str(i),
                    "id": i,
                }
                for i in range(3)
            ]
        )
        df.to_csv(ats_dir / "jobs.csv", index=False)
    return tmp_path


@pytest.fixture
def ats_csv_dir_with_duplicates(tmp_path: Path) -> Path:
    """Same shape as ``ats_csv_dir`` but the same company-title-location
    appears under both Workday (priority 1) and Eightfold (priority 5).

    Used to exercise cross-ATS dedup in the publisher.
    """
    # Workday: 3 unique jobs at AcmeCorp.
    workday_dir = tmp_path / "workday"
    workday_dir.mkdir()
    pd.DataFrame([
        {"url": f"https://workday.com/job/{i}", "title": f"Engineer {i}",
         "location": "Paris", "company": "AcmeCorp", "ats_id": str(i)}
        for i in range(3)
    ]).to_csv(workday_dir / "jobs.csv", index=False)

    # Eightfold mirrors all 3 (real-world: Eightfold sources the underlying
    # Workday). After dedup, only the Workday rows should survive.
    eightfold_dir = tmp_path / "eightfold"
    eightfold_dir.mkdir()
    pd.DataFrame([
        {"url": f"https://eightfold.com/job/{i}", "title": f"Engineer {i}",
         "location": "Paris", "company": "AcmeCorp", "ats_id": str(100 + i)}
        for i in range(3)
    ]).to_csv(eightfold_dir / "jobs.csv", index=False)

    return tmp_path


@pytest.fixture
def sample_manifest_dict() -> dict[str, Any]:
    return {
        "version": "1.0",
        "generated_at": "2026-05-03T00:00:00+00:00",
        "stats": {"total_jobs": 1000, "total_companies": 50, "ats_count": 2},
        "all": {
            "csv": "https://example.com/all.csv",
            "parquet": "https://example.com/all.parquet",
            "rows": 1000,
            "size_bytes": 1024,
        },
        "by_ats": {
            "greenhouse": {
                "csv": "https://example.com/gh.csv",
                "parquet": "https://example.com/gh.parquet",
                "rows": 600,
                "size_bytes": 700,
            },
            "lever": {
                "csv": "https://example.com/lever.csv",
                "rows": 400,
                "size_bytes": 500,
            },
        },
        "by_date": {
            "2026-05-03": {
                "csv": "https://example.com/2026-05-03.csv",
                "rows": 50,
                "size_bytes": 60,
            }
        },
        "companies": {
            "csv": "https://example.com/companies.csv",
            "rows": 50,
            "size_bytes": 100,
        },
    }


@pytest.fixture
def jobs_dataframe() -> pd.DataFrame:
    """A small but schema-realistic DataFrame for client/search tests."""
    return pd.DataFrame(
        [
            {
                "url": "https://example.com/1",
                "title": "Senior ML Engineer",
                "company": "OpenAI",
                "location": "San Francisco, CA",
                "is_remote": False,
                "ats_type": "ashby",
                "ats_id": "1",
                "salary_min": 200_000,
                "salary_max": 300_000,
                "experience": 5,
            },
            {
                "url": "https://example.com/2",
                "title": "Junior Backend Engineer",
                "company": "Stripe",
                "location": "Remote",
                "is_remote": True,
                "ats_type": "ashby",
                "ats_id": "2",
                "salary_min": 100_000,
                "salary_max": 140_000,
                "experience": 1,
            },
            {
                "url": "https://example.com/3",
                "title": "Staff ML Researcher",
                "company": "Anthropic",
                "location": "Paris, France",
                "is_remote": False,
                "ats_type": "greenhouse",
                "ats_id": "3",
                "salary_min": 250_000,
                "salary_max": 400_000,
                "experience": 8,
            },
            {
                "url": "https://example.com/4",
                "title": "Sales Engineer",
                "company": "Notion",
                "location": "Remote",
                "is_remote": True,
                "ats_type": "lever",
                "ats_id": "4",
                "salary_min": None,
                "salary_max": None,
                "experience": 3,
            },
        ]
    )


@pytest.fixture(autouse=True)
def _isolate_default_client_cache() -> Iterator[None]:
    """Reset the lru_cache on `_default_client` between tests so monkeypatched
    clients don't bleed across test modules."""
    from ats_scrapers import client as client_module

    client_module._default_client.cache_clear()
    yield
    client_module._default_client.cache_clear()
