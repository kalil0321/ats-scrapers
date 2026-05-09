"""Test the parquet normalization helper.

Regression test for the bug discovered on the first real upload: concatenating
per-ATS CSVs produced an `ats_id` column with mixed int/str values, which
pyarrow refuses to convert.

The helper mutates ``df`` in-place and returns it for chaining — at corpus
scale a defensive copy doubled peak memory.
"""

from __future__ import annotations

import pandas as pd

from jobhive.storage.publisher import _normalize_for_parquet_inplace


def test_object_columns_become_string_dtype() -> None:
    df = pd.DataFrame({"ats_id": [1, "abc", 2.5, None], "title": ["X", "Y", "Z", "W"]})
    out = _normalize_for_parquet_inplace(df)
    # pandas reports the dtype as either "string" (older) or "str" (3.0+);
    # what matters is that it's a pandas string-typed column.
    assert pd.api.types.is_string_dtype(out["ats_id"])
    assert pd.api.types.is_string_dtype(out["title"])


def test_numeric_columns_are_left_alone() -> None:
    df = pd.DataFrame({"salary_min": [100_000, 200_000], "lat": [37.7, 40.0]})
    out = _normalize_for_parquet_inplace(df)
    assert pd.api.types.is_integer_dtype(out["salary_min"])
    assert pd.api.types.is_float_dtype(out["lat"])


def test_mixed_int_and_string_ids_round_trip_through_parquet(tmp_path) -> None:
    pytest_arrow = __import__("importlib").util.find_spec("pyarrow")
    if pytest_arrow is None:
        return  # pyarrow optional in test env
    df = pd.DataFrame(
        {"ats_id": [1, "uuid-abc", 2], "title": ["A", "B", "C"], "company": ["x", "y", "z"]}
    )
    path = tmp_path / "out.parquet"
    _normalize_for_parquet_inplace(df).to_parquet(path, index=False)
    loaded = pd.read_parquet(path)
    assert loaded["ats_id"].tolist() == ["1", "uuid-abc", "2"]


def test_normalize_mutates_in_place_and_returns_same_frame() -> None:
    """The helper now mutates ``df`` and returns it — at corpus scale
    the previous defensive copy doubled peak memory. Caller is the
    publisher which does not reuse the original frame."""
    df = pd.DataFrame({"ats_id": [1, "abc"]})
    out = _normalize_for_parquet_inplace(df)
    assert out is df
    assert pd.api.types.is_string_dtype(df["ats_id"])
