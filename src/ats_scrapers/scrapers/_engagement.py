"""Read an explicit relationship from an ATS contract or employment-type label."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ats_scrapers.models import EngagementType

_LABELS: dict[str, EngagementType] = {
    "permanent": "permanent",
    "permanent employee": "permanent",
    "permanent employment": "permanent",
    "fixed term": "fixed_term",
    "fixed term contract": "fixed_term",
    "fixed term employment": "fixed_term",
    "independent contractor": "independent_contractor",
    "self employed": "independent_contractor",
    "selfemployed": "independent_contractor",
    "freelancer": "independent_contractor",
    "freelance": "independent_contractor",
    "temporary": "temporary",
    "temporary employment": "temporary",
    "apprentice": "apprenticeship",
    "apprenticeship": "apprenticeship",
    "intern": "internship",
    "internship": "internship",
}


def engagement_from_label(value: object) -> EngagementType | None:
    if not isinstance(value, str):
        return None
    label = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    # Pinpoint and Recruitee combine schedule and duration in either order.
    label = re.sub(r"^(?:full|part) ?time | (?:full|part) ?time$", "", label)
    return _LABELS.get(label)
