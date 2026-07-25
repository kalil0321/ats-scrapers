"""Shared parsing helpers for ByteDance's ATSx/Throne careers backend."""

from __future__ import annotations

import re
from typing import Any

from ats_scrapers.models import EmploymentType

_EMPLOYMENT_TYPE_PATTERNS: dict[str, EmploymentType] = {
    "intern": "INTERN",
    "internship": "INTERN",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "temporary": "TEMPORARY",
    "part-time": "PART_TIME",
    "part time": "PART_TIME",
    "parttime": "PART_TIME",
    "full-time": "FULL_TIME",
    "full time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "regular": "FULL_TIME",
    "permanent": "FULL_TIME",
}


def compose_description(*sources: object) -> str | None:
    """Concatenate description fields using the repository's 25k limit."""
    parts = [
        source.strip()
        for source in sources
        if isinstance(source, str) and source.strip()
    ]
    if not parts:
        return None
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip()
    return text[:25_000] or None


def extract_label(value: object) -> str | None:
    """Extract the preferred English label from a Throne field."""
    if not isinstance(value, dict):
        return None
    for key in ("en_name", "i18n_name", "name"):
        label = value.get(key)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def map_recruit_type(
    value: object,
) -> tuple[EmploymentType | None, str | None]:
    """Map a Throne recruit type to employment type and commitment."""
    label = extract_label(value)
    if not label:
        return None, None
    normalized = label.lower()
    for needle, mapped in _EMPLOYMENT_TYPE_PATTERNS.items():
        if needle in normalized:
            return mapped, label
    return None, label


def extract_location(item: dict[str, Any]) -> str | None:
    """Walk the current city parent chain, with legacy city-list fallback."""
    city_info = item.get("city_info")
    if isinstance(city_info, dict):
        parts: list[str] = []
        node: object = city_info
        while isinstance(node, dict):
            name = node.get("en_name") or node.get("name")
            if isinstance(name, str) and name:
                parts.append(name)
            node = node.get("parent")
        if parts:
            return ", ".join(parts)
    city_list = item.get("city_list") or []
    locations = [
        label
        for entry in city_list
        if (label := extract_label(entry)) is not None
    ]
    return "; ".join(dict.fromkeys(locations)) or None


def to_float(value: object) -> float | None:
    """Return a numeric API field as float when possible."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
