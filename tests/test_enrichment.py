"""Tests for the derived enrichment helpers."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from jobhive.enrichment.derived import (
    infer_is_remote,
    parse_salary_range,
)

# --- infer_is_remote --------------------------------------------------------

@pytest.mark.parametrize(
    "location",
    ["Remote", "remote", "Anywhere", "Distributed - US", "Work from home", "WFH only"],
)
def test_remote_keywords_detected(location: str) -> None:
    assert infer_is_remote(location) is True


@pytest.mark.parametrize(
    "location",
    ["Onsite — NYC", "On-site, San Francisco", "In-office, Berlin"],
)
def test_onsite_keywords_detected(location: str) -> None:
    assert infer_is_remote(location) is False


@pytest.mark.parametrize("location", ["Paris", "London", "Tokyo, Japan", ""])
def test_neutral_locations_return_none(location: str) -> None:
    assert infer_is_remote(location) is None


@pytest.mark.parametrize("value", [None, math.nan, 0, 12.5, [], {}, object()])
def test_non_string_values_return_none(value: object) -> None:
    """NaN and other non-string types must not crash the function."""
    assert infer_is_remote(value) is None


def test_handles_pandas_nan_in_series() -> None:
    """Regression: pandas .apply() passes NaN floats for empty cells."""
    series = pd.Series(["Remote", None, float("nan"), "Paris"])
    result = series.apply(infer_is_remote)
    assert result.tolist() == [True, None, None, None]


# --- parse_salary_range -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$257K - $335K", (257_000.0, 335_000.0)),
        ("CA$400K – CA$500K", (400_000.0, 500_000.0)),
        ("€80k–€120k", (80_000.0, 120_000.0)),
        ("$60K to $80K", (60_000.0, 80_000.0)),
        ("$200,000 - $300,000", (200_000.0, 300_000.0)),
        ("OTE $1.5M - $2M", (1_500_000.0, 2_000_000.0)),
        ("$100K", (100_000.0, 100_000.0)),
    ],
)
def test_parse_salary_range_known_formats(text: str, expected: tuple[float, float]) -> None:
    lo, hi = parse_salary_range(text)
    assert lo == pytest.approx(expected[0])
    assert hi == pytest.approx(expected[1])


@pytest.mark.parametrize(
    "text",
    [None, "", "Competitive", "Negotiable", "DOE", "Based on experience", float("nan")],
)
def test_parse_salary_range_returns_none_when_unparseable(text: object) -> None:
    assert parse_salary_range(text) == (None, None)


def test_parse_salary_range_swaps_inverted_bounds() -> None:
    """`max - min` should be normalized to `(min, max)`."""
    lo, hi = parse_salary_range("$300K - $200K")
    assert (lo, hi) == (200_000.0, 300_000.0)
