"""Dataset manifest — the single source of truth for what data is published.

The manifest lives at a stable URL (`DEFAULT_MANIFEST_URL`) and points at every
other artifact (full snapshot, per-ATS slices, per-day deltas, companies). The
client always fetches the manifest first so we can rotate underlying file names
or add new shards without breaking installed clients.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

from jobhive.exceptions import ManifestError
from jobhive.models import ATSType

DEFAULT_MANIFEST_URL = "https://storage.stapply.ai/jobhive/v1/manifest.json"


class FileEntry(BaseModel):
    """One artifact in the manifest.

    `csv` / `parquet` may be either a full URL (when the publisher has a
    public CDN base) or a relative R2 object key (development / private
    buckets). The client side resolves both.
    """

    model_config = ConfigDict(frozen=True)

    csv: str | None = None
    parquet: str | None = None
    rows: int = Field(..., ge=0)
    size_bytes: int = Field(..., ge=0)
    sha256: str | None = None


class ManifestStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_jobs: int
    total_companies: int
    ats_count: int


class Manifest(BaseModel):
    """Top-level manifest of every artifact jobhive publishes.

    Versioned on `version` so we can ship breaking layout changes by bumping it
    and serving both versions in parallel during a deprecation window.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "1.0"
    generated_at: datetime
    stats: ManifestStats
    all: FileEntry
    by_ats: dict[ATSType, FileEntry] = Field(default_factory=dict)
    by_date: dict[str, FileEntry] = Field(default_factory=dict)
    companies: FileEntry | None = None

    @classmethod
    def fetch(
        cls,
        url: str = DEFAULT_MANIFEST_URL,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> Self:
        """Fetch and validate the manifest from a URL."""
        owns_client = client is None
        client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        try:
            response = client.get(url)
            response.raise_for_status()
            return cls.model_validate(response.json())
        except httpx.HTTPError as exc:
            raise ManifestError(f"Failed to fetch manifest from {url}: {exc}") from exc
        except ValueError as exc:
            raise ManifestError(f"Manifest at {url} is not valid JSON or schema: {exc}") from exc
        finally:
            if owns_client:
                client.close()

    def url_for_ats(self, ats: ATSType, *, prefer_parquet: bool = True) -> str:
        """Return the best download URL for a given ATS slice."""
        entry = self.by_ats.get(ats)
        if entry is None:
            raise ManifestError(f"ATS {ats} is not present in manifest")
        return _pick_url(entry, prefer_parquet=prefer_parquet)

    def url_for_all(self, *, prefer_parquet: bool = True) -> str:
        """Return the best download URL for the full snapshot."""
        return _pick_url(self.all, prefer_parquet=prefer_parquet)

    def url_for_date(self, date: str, *, prefer_parquet: bool = True) -> str:
        """Return the best download URL for a dated snapshot."""
        entry = self.by_date.get(date)
        if entry is None:
            raise ManifestError(f"No snapshot for date {date}")
        return _pick_url(entry, prefer_parquet=prefer_parquet)


def _pick_url(entry: FileEntry, *, prefer_parquet: bool) -> str:
    if prefer_parquet and entry.parquet is not None:
        return str(entry.parquet)
    if entry.csv is not None:
        return str(entry.csv)
    if entry.parquet is not None:
        return str(entry.parquet)
    raise ManifestError("File entry has neither csv nor parquet URL")
