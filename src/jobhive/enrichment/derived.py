"""Derive enrichment columns from existing fields.

Pure functions, cheap to run, no network. Deliberately narrow:

- We only return ``True`` for ``is_remote`` when the **title** carries
  an unambiguous remote marker. The absence of such a marker is *not*
  evidence the role is on-site, so we return ``None`` (unknown) rather
  than ``False``.
- We do not parse the description for remote signals — phrasing there
  is too ambiguous for a hardcoded rule. LLM-based enrichment
  downstream fills the rest.
- Salary range parsing on free text is preserved (see
  :func:`parse_salary_range`) — the regex is tight and the input is
  conventionally structured.
"""

from __future__ import annotations

import re

# Markers we treat as definitive when they appear in a title.
# Conservative on purpose — "Remote Engineer" is unambiguous; titles
# like "Remote Sales Director" also fire True and the role really is
# remote in those cases.
#
# ``distributed`` is intentionally excluded: titles like
# "Distributed Systems Engineer" / "Senior Engineer, Distributed
# Storage" use the word as a technical-domain qualifier (distributed
# computing) rather than a workforce-placement signal. The downstream
# LLM enrichment pipeline can still classify such roles as remote
# from the full posting context if they actually are.
REMOTE_KEYWORDS = (
    "remote",
    "anywhere",
    "work from home",
    "wfh",
    "telework",
)


def infer_is_remote(title: object) -> bool | None:
    """Return ``True`` when the title contains a remote marker, else
    ``None``.

    Never returns ``False`` — the absence of "remote" in the title is
    not evidence the role is on-site. Many remote roles have plain
    titles like "Senior Engineer". LLM-based enrichment downstream is
    expected to fill ``True`` / ``False`` from the full posting
    context.
    """
    if not isinstance(title, str) or not title.strip():
        return None
    if any(kw in title.lower() for kw in REMOTE_KEYWORDS):
        return True
    return None


# --- Salary parsing ---------------------------------------------------------

_SALARY_RANGE_RE = re.compile(
    r"""
    (?P<sym1>[$£€¥]|CA\$|US\$|A\$|NZ\$|HK\$|S\$|R\$)?\s*
    (?P<n1>\d[\d,. ]*)\s*
    (?P<u1>[KMkm]|thousand|million)?
    \s*(?:[-–—~]|to)\s*
    (?P<sym2>[$£€¥]|CA\$|US\$|A\$|NZ\$|HK\$|S\$|R\$)?\s*
    (?P<n2>\d[\d,. ]*)\s*
    (?P<u2>[KMkm]|thousand|million)?
    """,
    re.VERBOSE,
)
_SALARY_SINGLE_RE = re.compile(
    r"""
    (?P<sym>[$£€¥]|CA\$|US\$|A\$)?\s*
    (?P<n>\d[\d,. ]{2,})\s*
    (?P<u>[KMkm]|thousand|million)?
    """,
    re.VERBOSE,
)


def _parse_salary_token(num: str, unit: str | None) -> float | None:
    """Convert a number token + optional unit suffix to a float amount."""
    cleaned = num.replace(",", "").replace(" ", "").rstrip(".")
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1)
    try:
        value = float(cleaned)
    except ValueError:
        return None
    multiplier = 1.0
    if unit:
        u = unit.lower()
        if u.startswith("k") or u == "thousand":
            multiplier = 1_000
        elif u.startswith("m") or u == "million":
            multiplier = 1_000_000
    return value * multiplier


def parse_salary_range(text: object) -> tuple[float | None, float | None]:
    """Extract `(min, max)` from a salary summary string.

    Handles `$257K - $335K`, `CA$400K – CA$500K`, `60,000 - 80,000`,
    `€80k–€120k`, etc. Returns (None, None) when nothing parseable.
    """
    if not isinstance(text, str) or not text.strip():
        return (None, None)
    match = _SALARY_RANGE_RE.search(text)
    if match:
        lo = _parse_salary_token(match.group("n1"), match.group("u1"))
        hi = _parse_salary_token(match.group("n2"), match.group("u2"))
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        return (lo, hi)
    match = _SALARY_SINGLE_RE.search(text)
    if match:
        value = _parse_salary_token(match.group("n"), match.group("u"))
        return (value, value)
    return (None, None)
