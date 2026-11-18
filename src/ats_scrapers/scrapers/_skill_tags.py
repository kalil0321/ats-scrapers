"""Map native skill tags on a job posting."""

from __future__ import annotations

from ats_scrapers.models import RequirementLevel, SkillRequirement
from ats_scrapers.scrapers._job_classification import classification_label


def skill_tags(
    values: object, *, label_key: str | None = None, requirement: RequirementLevel = "unspecified",
    existing: list[SkillRequirement] | None = None,
) -> list[SkillRequirement] | None:
    """Read source skill labels, preserving their order and any explicit group priority."""
    if not isinstance(values, list):
        return existing
    skills = list(existing or [])
    labels = {skill.label for skill in skills}
    for entry in values:
        if label_key is not None:
            if not isinstance(entry, dict):
                continue
            entry = entry.get(label_key)
        label = classification_label(entry)
        if label is not None and label not in labels:
            skills.append(SkillRequirement(label=label, requirement=requirement))
            labels.add(label)
    return skills or None
