from datetime import UTC, datetime

import pytest

from ats_scrapers.scrapers._source_timestamp import source_timestamp


@pytest.mark.parametrize("value", [
    None, True, 1700000000, "", "2 minutes", "2026-11-10", "2026-11-10 UTC",
    "2026-11-10T01:30:00", "2026-11-31T01:30:00Z", "2026-11-10T01:30:00-05:75",
    "2026-11-10T01:30:00.1234567Z", "0001-01-01T00:00:00+01:00",
])
def test_unusable_clock_stays_empty(value) -> None:
    assert source_timestamp(value) is None


def test_offset_conversion_preserves_day_and_fraction() -> None:
    value = source_timestamp("2026-04-01T00:30:00.1234560+02:00")
    assert value is not None
    assert value == datetime(2026, 3, 31, 22, 30, 0, 123456, tzinfo=UTC)
    assert value.tzinfo is UTC
