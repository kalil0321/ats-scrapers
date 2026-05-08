"""Enrichment — salary parsing, geocoding, classification.

The legacy implementations live at the repo root in
`extract_salary_experience.py` and `classifier/`. This module will wrap them
progressively.
"""

from jobhive.enrichment.derived import (
    infer_is_remote,
    parse_salary_range,
)

__all__ = ["infer_is_remote", "parse_salary_range"]
