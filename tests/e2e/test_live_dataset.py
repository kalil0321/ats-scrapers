"""Live end-to-end tests for the hosted-dataset client.

Opt in with ``ATS_SCRAPERS_LIVE_E2E=1``. Downloads the manifest and one
small per-source slice — never the multi-gigabyte full snapshot.
"""

from __future__ import annotations

import os

import pytest

from ats_scrapers import Client, list_ats

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to hit the hosted dataset",
    ),
]


def test_live_manifest_lists_sources() -> None:
    sources = list(list_ats())
    assert "greenhouse" in sources
    assert len(sources) >= 40


def test_live_slice_search_returns_jobs() -> None:
    with Client() as client:
        df = client.search(query="engineer", ats="greenhouse", limit=5)
    assert not df.empty
    row = df.iloc[0]
    assert row["title"] and str(row["url"]).startswith("http")
    assert "global_id" in df.columns
