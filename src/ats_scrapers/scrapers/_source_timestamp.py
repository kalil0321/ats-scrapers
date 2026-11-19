"""Parse explicit posting-record clocks from public provider payloads."""

from __future__ import annotations

import re
from datetime import UTC, datetime

# datetime preserves microseconds. Additional fractional digits must be zero
# for the source instant to be exactly representable.
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6}0*)?"
    r"(?:Z| UTC|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def source_timestamp(value: object) -> datetime | None:
    """Return an exact UTC instant for an offset-bearing source timestamp."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _TIMESTAMP.fullmatch(text):
        return None
    if text.endswith(" UTC"):
        text = text.removesuffix(" UTC") + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except (OverflowError, ValueError):
        return None
