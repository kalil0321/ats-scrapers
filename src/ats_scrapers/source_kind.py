"""Classify each source by where its postings come from.

The dataset mixes four kinds of sources:

- ``ats``: an applicant tracking system hosting the employer's own board
  (Workday, Greenhouse, SmartRecruiters, ...).
- ``company_site``: one employer's own careers system (Amazon, Google, ...).
- ``gov_board``: a public employment service that republishes postings
  from many employers (EURES, Bundesagentur, Job Bank, ...).
- ``job_board``: a commercial job board or marketplace where employers
  post (Welcome to the Jungle, Wellfound, Remote OK, ...). Mercor and Gem
  are here too: the publisher's dedup priorities already treat both as
  hybrid job boards, and Mercor is one feed for many employers.

The headline job count covers ``ats`` and ``company_site`` only: those
postings come straight from the employer's hiring system.

Any source not listed below counts as ``ats``. A new board or public
service must therefore be added here explicitly, or it would inflate the
headline.
"""

from __future__ import annotations

from typing import Final, Literal

SourceKind = Literal["ats", "company_site", "gov_board", "job_board"]

HEADLINE_KINDS: Final[frozenset[str]] = frozenset({"ats", "company_site"})

COMPANY_SITES: Final[frozenset[str]] = frozenset({
    "amazon", "apple", "bytedance", "google", "meta", "tesla", "tiktok",
    "uber",
})

GOV_BOARDS: Final[frozenset[str]] = frozenset({
    "arbetsformedlingen", "bundesagentur", "eures", "jobbankca", "usajobs",
})

JOB_BOARDS: Final[frozenset[str]] = frozenset({
    "builtin", "gem", "getonbrd", "infojobs_es", "jobs_cz", "jobsch",
    "manfred", "mercor", "programathor", "remoteok", "seek", "thehub",
    "wanted", "wellfound", "welcometothejungle", "weworkremotely",
    "ycombinator",
})


def source_kind(source: str) -> SourceKind:
    """Return the kind of ``source`` (an ``ats_type`` value)."""
    key = source.strip().lower()
    if key in COMPANY_SITES:
        return "company_site"
    if key in GOV_BOARDS:
        return "gov_board"
    if key in JOB_BOARDS:
        return "job_board"
    return "ats"


def is_headline(source: str) -> bool:
    """Whether postings from ``source`` count toward the headline total."""
    return source_kind(source) in HEADLINE_KINDS
