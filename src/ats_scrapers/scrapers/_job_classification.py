"""Map source classifications without inferring qualifications from job text."""

from __future__ import annotations

from typing import Literal

from ats_scrapers.models import EducationRequirement, Occupation


def classification_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    if label.casefold() in {
        "", "-", "n/a", "none", "not applicable", "not specified", "other", "unknown", "unspecified",
    }:
        return None
    return label


def occupation(label: object, *, taxonomy: str, code: object = None) -> Occupation | None:
    name = classification_label(label)
    if name is None:
        return None
    identifier = code.strip() if isinstance(code, str) else None
    return Occupation(label=name, code=identifier or None, taxonomy=taxonomy)


def education_level(
    value: object, *, requirement: Literal["required", "unspecified"] = "unspecified"
) -> list[EducationRequirement] | None:
    label = classification_label(value)
    if label is None or label.casefold() in {
        "not required", "no requirement", "equivalent experience",
        "expérience équivalente", "gleichwertige erfahrung", "experiencia equivalente",
    }:
        return None
    return [EducationRequirement(label=label, requirement=requirement)]
