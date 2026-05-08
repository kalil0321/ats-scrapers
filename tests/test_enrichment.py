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
#
# Title-only inference; never returns False (absence of keyword in
# title is not evidence the role is on-site, LLM downstream fills
# that nuance).


@pytest.mark.parametrize(
    "title",
    [
        "Remote Software Engineer",
        "remote backend developer",
        "Anywhere — Senior Engineer",
        "Distributed Systems Engineer (Remote)",
        "Work from home — Customer Success",
        "WFH Sales Rep",
        "Telework Researcher",
    ],
)
def test_remote_keywords_in_title_detected(title: str) -> None:
    assert infer_is_remote(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Customer Success Manager",
        "Backend Engineer, NYC",
        "Onsite Recruiter — SF",  # no remote keyword in title; we don't infer False from "onsite"
        "In-office Designer",  # ditto — never return False
    ],
)
def test_titles_without_remote_marker_return_none(title: str) -> None:
    """Never assert False from heuristic. The absence of a remote
    keyword is not evidence of on-site."""
    assert infer_is_remote(title) is None


@pytest.mark.parametrize("title", ["", "   ", "\t"])
def test_empty_title_returns_none(title: str) -> None:
    assert infer_is_remote(title) is None


@pytest.mark.parametrize("value", [None, math.nan, 0, 12.5, [], {}, object()])
def test_non_string_values_return_none(value: object) -> None:
    """NaN and other non-string types must not crash the function."""
    assert infer_is_remote(value) is None


def test_handles_pandas_nan_in_series() -> None:
    """Regression: pandas .apply() passes NaN floats for empty cells."""
    series = pd.Series([
        "Remote Engineer",
        None,
        float("nan"),
        "Senior Manager",
    ])
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
