"""Layer 1: dataset client.

Reads the published Cloudflare-hosted snapshot and returns a pandas DataFrame.
This is the path almost every user will take — `from jobhive import search`.

Implementation notes:
- Caches manifest + downloaded snapshot in memory for the process lifetime.
- Filters happen client-side on the loaded DataFrame; the dataset is small
  enough (~50-500 MB compressed) that this is faster than a server roundtrip.
- For large-scale or real-time use, swap `Client` for the per-ATS scrapers.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.util import find_spec
from io import BytesIO
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from jobhive.exceptions import StorageError
from jobhive.manifest import DEFAULT_MANIFEST_URL, Manifest
from jobhive.models import ATSType

if TYPE_CHECKING:
    from collections.abc import Iterable


class Client:
    """Read-side client for the public jobhive dataset.

    >>> client = Client()
    >>> df = client.search(query="rust", remote=True)

    Pass a custom `manifest_url` to point at a fork or staging environment.
    """

    def __init__(
        self,
        *,
        manifest_url: str = DEFAULT_MANIFEST_URL,
        prefer_parquet: bool | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._manifest_url = manifest_url
        self._prefer_parquet = _has_parquet_engine() if prefer_parquet is None else prefer_parquet
        self._http_client = http_client or httpx.Client(
            timeout=120.0, follow_redirects=True
        )
        self._owns_http = http_client is None
        self._manifest: Manifest | None = None
        self._snapshot: pd.DataFrame | None = None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http_client.close()

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest.fetch(self._manifest_url, client=self._http_client)
        return self._manifest

    def load(
        self,
        *,
        ats: ATSType | str | None = None,
        date: str | None = None,
    ) -> pd.DataFrame:
        """Load a slice of the dataset as a DataFrame.

        Without arguments, loads the full snapshot. `ats` loads one ATS slice
        (much smaller). `date` loads a single day's delta.
        """
        if ats is not None and date is not None:
            raise ValueError("Pass either `ats` or `date`, not both")

        if ats is not None:
            ats_enum = ATSType(ats) if isinstance(ats, str) else ats
            url = self.manifest.url_for_ats(ats_enum, prefer_parquet=self._prefer_parquet)
            return self._download(url)

        if date is not None:
            url = self.manifest.url_for_date(date, prefer_parquet=self._prefer_parquet)
            return self._download(url)

        if self._snapshot is None:
            url = self.manifest.url_for_all(prefer_parquet=self._prefer_parquet)
            self._snapshot = self._download(url)
        return self._snapshot

    def search(
        self,
        query: str | None = None,
        *,
        location: str | None = None,
        company: str | None = None,
        ats: ATSType | str | None = None,
        remote: bool | None = None,
        salary_min: float | None = None,
        salary_max: float | None = None,
        experience_max: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Filter the snapshot by common criteria.

        All string filters are case-insensitive substring matches. Salary
        filters compare against `salary_min`/`salary_max` columns when present.
        """
        df = self.load(ats=ats)

        if query:
            df = df[df["title"].str.contains(query, case=False, na=False)]
        if location:
            df = df[df["location"].fillna("").str.contains(location, case=False, na=False)]
        if company:
            df = df[df["company"].str.contains(company, case=False, na=False)]
        if remote is True and "location" in df.columns:
            df = df[df["location"].fillna("").str.contains("remote", case=False, na=False)]
        if salary_min is not None and "salary_max" in df.columns:
            df = df[df["salary_max"].fillna(0) >= salary_min]
        if salary_max is not None and "salary_min" in df.columns:
            df = df[df["salary_min"].fillna(float("inf")) <= salary_max]
        if experience_max is not None and "experience" in df.columns:
            df = df[df["experience"].fillna(0) <= experience_max]

        if limit is not None:
            df = df.head(limit)
        return df.reset_index(drop=True)

    def _download(self, url: str) -> pd.DataFrame:
        try:
            response = self._http_client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StorageError(f"Failed to download {url}: {exc}") from exc

        buffer = BytesIO(response.content)
        if url.endswith(".parquet"):
            try:
                return pd.read_parquet(buffer)
            except ImportError as exc:
                raise StorageError(
                    "This dataset artifact is Parquet-only, but no Parquet engine "
                    "is installed. Install with `pip install jobhive-py[parquet]`, "
                    "or load a per-ATS CSV slice with `Client(prefer_parquet=False).load(ats=...)`."
                ) from exc
        return pd.read_csv(buffer)


@lru_cache(maxsize=1)
def _default_client() -> Client:
    return Client()


def _has_parquet_engine() -> bool:
    return find_spec("pyarrow") is not None or find_spec("fastparquet") is not None


def search(
    query: str | None = None,
    *,
    location: str | None = None,
    company: str | None = None,
    ats: ATSType | str | None = None,
    remote: bool | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    experience_max: int | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """One-shot convenience wrapper around `Client.search`.

    The default client is cached process-wide so repeated calls reuse the
    downloaded snapshot.
    """
    return _default_client().search(
        query,
        location=location,
        company=company,
        ats=ats,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        experience_max=experience_max,
        limit=limit,
    )


def list_ats() -> Iterable[ATSType]:
    """Return the ATS platforms with data in the current manifest."""
    return _default_client().manifest.by_ats.keys()
