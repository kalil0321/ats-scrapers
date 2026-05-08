"""Core data models for jobs, companies, and salary information.

These models are the canonical schema across every ATS scraper and the
public dataset on storage.stapply.ai. Adding a field here means: the
dataset gets a new column, every scraper must populate it (or leave
it None), and the parquet schema gets a new field.

The Job schema is also documented for human readers in
``JOB_SCHEMA.md`` at the repo root — keep the two in sync. ``Field``
descriptions in this file are the source of truth; the markdown is a
view onto them.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

log = logging.getLogger(__name__)


class ATSType(StrEnum):
    """Supported applicant tracking systems.

    A company belongs to exactly one ATS. The ATS determines which
    scraper knows how to fetch its jobs and how the careers page is
    structured.
    """

    ASHBY = "ashby"
    AVATURE = "avature"
    CORNERSTONE = "cornerstone"
    EIGHTFOLD = "eightfold"
    GEM = "gem"
    GREENHOUSE = "greenhouse"
    ICIMS = "icims"
    JOIN_COM = "join_com"
    LEVER = "lever"
    MERCOR = "mercor"
    ORACLE = "oracle"
    PERSONIO = "personio"
    PHENOM = "phenom"
    PINPOINT = "pinpoint"
    RECRUITERBOX = "recruiterbox"
    RIPPLING = "rippling"
    SMARTRECRUITERS = "smartrecruiters"
    SUCCESSFACTORS = "successfactors"
    WORKABLE = "workable"
    WORKDAY = "workday"
    # Big-tech custom careers systems (single-tenant, bespoke APIs)
    AMAZON = "amazon"
    APPLE = "apple"
    GOOGLE = "google"
    META = "meta"
    TESLA = "tesla"
    TIKTOK = "tiktok"
    UBER = "uber"
    USAJOBS = "usajobs"
    # National public-sector job boards (single-source, single-tenant
    # scrapers — each is the entire country's jobs api)
    BUNDESAGENTUR = "bundesagentur"
    ARBETSFORMEDLINGEN = "arbetsformedlingen"
    EURES = "eures"
    # Hybrid jobboards (companies post directly, not aggregated)
    WELCOMETOTHEJUNGLE = "welcometothejungle"
    GETONBRD = "getonbrd"
    WANTED = "wanted"
    REMOTEOK = "remoteok"
    WEWORKREMOTELY = "weworkremotely"
    PROGRAMATHOR = "programathor"
    BUILTIN = "builtin"
    JOBSCH = "jobsch"
    MANFRED = "manfred"
    THEHUB = "thehub"
    THEMUSE = "themuse"
    YCOMBINATOR = "ycombinator"
    WELLFOUND = "wellfound"
    # Additional multi-tenant ATSes (post-0.1)
    BAMBOOHR = "bamboohr"
    BREEZY = "breezy"
    JAZZHR = "jazzhr"
    JOBVITE = "jobvite"
    RECRUITEE = "recruitee"
    TALEO = "taleo"
    TEAMTAILOR = "teamtailor"
    CUSTOM = "custom"


SalaryPeriod = Literal["HOUR", "DAY", "WEEK", "MONTH", "YEAR"]


class Salary(BaseModel):
    """Compensation range attached to a job posting.

    Stored separately from ``Job`` so the same shape can be reused for
    total comp, base, equity, etc. — currently only base is populated.
    """

    model_config = ConfigDict(frozen=True)

    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 code")
    period: SalaryPeriod = "YEAR"
    min_amount: float | None = None
    max_amount: float | None = None
    summary: str | None = Field(None, description="Original string as displayed by the ATS")


class Company(BaseModel):
    """A company tracked by jobhive."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(..., description="ATS-specific identifier (e.g. 'openai' on Ashby)")
    name: str
    ats: ATSType
    careers_url: HttpUrl | None = None
    website: HttpUrl | None = None


EmploymentType = Literal["FULL_TIME", "PART_TIME", "CONTRACT", "INTERN", "TEMPORARY"]


# Control characters that would corrupt a CSV / JSON line if they
# made it into ``ats_id`` (and thus ``global_id``). Newline / tab /
# carriage return / NULL — anything else printable stays.
_ATS_ID_FORBIDDEN_CHARS = re.compile(r"[\x00\t\r\n]")


class Job(BaseModel):
    """A job posting — the canonical row across the entire dataset.

    Every scraper produces ``Job`` instances; the public CSV/Parquet
    exports use these field names verbatim. **Backwards compatibility
    on field names is part of the public contract** — renaming a field
    is a breaking change.

    The fields fall into four groups, listed in the order they appear
    below:

    1. **Identity** (``global_id``, ``url``, ``title``, ``company``,
       ``ats_type``, ``ats_id``). What the row *is*.

    2. **Location** (``location``, ``lat``, ``lon``, ``is_remote``).
       Where the role lives. ``is_remote`` is derived from
       ``location`` text by ``jobhive.enrichment.infer_is_remote``
       when the ATS doesn't surface a flag of its own.

    3. **Compensation** (``salary_currency``, ``salary_period``,
       ``salary_summary``, ``salary_min``, ``salary_max``).
       ``salary_min`` / ``salary_max`` are *derived* from
       ``salary_summary`` via ``jobhive.enrichment.parse_salary_range``
       when the ATS exposes only free text.

    4. **Classification** (``experience``, ``employment_type``,
       ``department``, ``team``, ``requisition_id``, ``apply_url``,
       ``commitment``). Optional — set when the source API exposes
       them, ``None`` otherwise.

    5. **Content & timing** (``description``, ``posted_at``,
       ``fetched_at``).

    6. **Provider-specific overflow** (``raw``): a JSON dict captured
       at scrape-time so we don't lose ATS-specific fields the
       canonical schema can't represent (Greenhouse ``metadata``
       custom fields, Bundesagentur ``arbeitszeit``/``branche``,
       Lever ``categories.*``, etc.).
    """

    model_config = ConfigDict(populate_by_name=True)

    # --- Identity ---------------------------------------------------------

    global_id: str = Field(
        default="",
        description=(
            "Globally unique identifier for the posting, formatted as "
            "``{ats_type}:{ats_id}`` when both are set (e.g. "
            "``ashby:engineer-2026`` or ``workday:R0136150``). The "
            "separator is a colon — parsers should split on the FIRST "
            "colon since ``ats_id`` may itself contain colons. When "
            "``ats_id`` is missing, malformed, or contains control "
            "characters, falls back to a random UUID4 and an error is "
            "logged. Populated automatically by a model validator; do "
            "not pass this field to ``Job(...)`` constructors."
        ),
    )
    url: HttpUrl = Field(
        ...,
        description=(
            "Public posting URL on the ATS. Always present. The "
            "primary stable identifier consumers should use to "
            "deduplicate or link out to the live page."
        ),
    )
    title: str = Field(
        ...,
        description=(
            "Free-form job title as posted (e.g. ``Senior Software "
            "Engineer, Reality Labs``). May contain spaces, punctuation, "
            "or non-ASCII characters."
        ),
    )
    company: str = Field(
        ...,
        description=(
            "Display name of the hiring employer. Distinct from "
            "``ats_id``: the same company can have ``company='OpenAI'`` "
            "and ``ats_id='openai'`` on Ashby. Different ATSes use "
            "different conventions — Greenhouse stores a numeric board "
            "id, Workday stores the human-readable name, Oracle the "
            "host, etc. — so don't depend on this field for cross-ATS "
            "joining; use ``ats_type``+``ats_id`` instead."
        ),
    )
    ats_type: ATSType = Field(
        ...,
        alias="ats_type",
        description=(
            "Which ATS platform serves this posting. Determines the "
            "scraper that produced the row and the format of "
            "``ats_id``."
        ),
    )
    ats_id: str | None = Field(
        default=None,
        description=(
            "Per-ATS identifier for the posting — Greenhouse numeric "
            "id, Workday requisition slug, Lever UUID, etc. Unique "
            "within ``ats_type`` but not globally (use ``global_id`` "
            "for that). Optional defensively: when null/empty/malformed, "
            "``global_id`` falls back to UUID4 instead of crashing the "
            "row, and an error is logged so the broken scraper is "
            "noticed."
        ),
    )

    # --- Location ---------------------------------------------------------

    location: str | None = Field(
        default=None,
        description=(
            "Free-form location string as posted (e.g. ``Paris, France``, "
            "``Remote — US``, ``Berlin or Remote``). Multi-location "
            "postings are rendered as comma-joined when the ATS "
            "exposes a list."
        ),
    )
    lat: float | None = Field(
        default=None,
        description=(
            "Latitude in WGS-84 degrees when the ATS provides "
            "geocoded coordinates (rare — most don't). Not derived "
            "from ``location`` text."
        ),
    )
    lon: float | None = Field(
        default=None,
        description=(
            "Longitude in WGS-84 degrees. See ``lat`` notes — "
            "populated together or not at all."
        ),
    )
    is_remote: bool | None = Field(
        default=None,
        description=(
            "Whether the role can be performed remotely. Set by the "
            "scraper when the ATS exposes a flag, otherwise derived "
            "from ``location`` text via "
            "``jobhive.enrichment.infer_is_remote`` at publish time. "
            "``None`` means we genuinely don't know."
        ),
    )

    # --- Compensation -----------------------------------------------------

    salary_currency: str | None = Field(
        default=None,
        description=(
            "ISO 4217 currency code (``USD``, ``EUR``, ``GBP``, …) "
            "when the ATS surfaces a structured salary range. ``None`` "
            "when the salary is absent OR present only as free text "
            "(in that case ``salary_summary`` is set)."
        ),
    )
    salary_period: SalaryPeriod | None = Field(
        default=None,
        description=(
            "Period the salary applies to. ``YEAR`` is the most common; "
            "``HOUR`` shows up on hourly/contractor postings."
        ),
    )
    salary_summary: str | None = Field(
        default=None,
        description=(
            "Original salary string as the ATS displays it (e.g. "
            "``$120K – $160K``, ``45.000 € / Jahr``, ``up to £80k``). "
            "Source-of-truth when ``salary_min``/``salary_max`` are "
            "derived from this field rather than provided directly."
        ),
    )
    salary_min: float | None = Field(
        default=None,
        description=(
            "Lower bound of the salary range, in ``salary_currency``. "
            "Either set directly by the scraper from a structured ATS "
            "field, or derived from ``salary_summary`` via "
            "``jobhive.enrichment.parse_salary_range`` at publish time."
        ),
    )
    salary_max: float | None = Field(
        default=None,
        description=(
            "Upper bound of the salary range, in ``salary_currency``. "
            "Same population logic as ``salary_min``."
        ),
    )

    # --- Classification ---------------------------------------------------

    experience: int | None = Field(
        default=None,
        description=(
            "Required years of experience as an integer when the ATS "
            "exposes a structured value. ``None`` when missing or "
            "only described in prose."
        ),
    )
    employment_type: EmploymentType | None = Field(
        default=None,
        description=(
            "Normalized employment type — one of ``FULL_TIME``, "
            "``PART_TIME``, ``CONTRACT``, ``INTERN``, ``TEMPORARY``. "
            "Cross-ATS comparable; use this for filtering. The "
            "ATS-specific raw label lives in ``commitment``."
        ),
    )
    department: str | None = Field(
        default=None,
        description=(
            "High-level org grouping (``Engineering``, ``Sales``, "
            "``Marketing``, …) when the ATS surfaces it. Distinct "
            "from ``team`` which is finer-grained."
        ),
    )
    team: str | None = Field(
        default=None,
        description=(
            "Sub-team / squad within the department (``Reality "
            "Labs``, ``Payments Infra``, …). Often empty even when "
            "``department`` is set."
        ),
    )
    requisition_id: str | None = Field(
        default=None,
        description=(
            "Employer-internal requisition identifier (Greenhouse "
            "``requisition_id``, Workday ``bulletFields[0]``, Lever's "
            "private id, Bundesagentur ``hashId``). Distinct from "
            "``ats_id`` which is platform-side. Same role mirrored on "
            "two different ATSes shares the same ``requisition_id`` "
            "but has two different ``ats_id`` — strong cross-ATS dedup "
            "signal."
        ),
    )
    apply_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Direct application URL when distinct from the posting "
            "``url``. Some ATSes (Workable widget, Bundesagentur "
            "external boards, YC's workatastartup) redirect to a "
            "separate apply destination."
        ),
    )
    commitment: str | None = Field(
        default=None,
        description=(
            "Free-form commitment label from the source ATS (Lever's "
            "``commitment``, Workable's ``type``, Bundesagentur's "
            "``arbeitszeit`` description, ``CDI``/``CDD``, ``Heltid``, "
            "``32h/week``, …). Distinct from ``employment_type`` which "
            "is the normalized enum — keep ``commitment`` to preserve "
            "language and granularity (hours, contract length, …) the "
            "enum loses."
        ),
    )

    # --- Content & timing -------------------------------------------------

    description: str | None = Field(
        default=None,
        description=(
            "Plain-text job description. HTML and markdown are "
            "stripped to text. Truncated to ~10kB when the source "
            "exceeds it."
        ),
    )
    posted_at: datetime | None = Field(
        default=None,
        description=(
            "When the ATS reports the posting was first published. "
            "UTC. ``None`` when the ATS doesn't expose this — common "
            "on aggregator sites and some legacy ATSes."
        ),
    )
    fetched_at: datetime | None = Field(
        default=None,
        description="When jobhive last saw this posting (UTC).",
    )

    # --- Provider-specific overflow ---------------------------------------

    raw: dict[str, object] | None = Field(
        default=None,
        description=(
            "Provider-specific overflow fields kept verbatim "
            "(Greenhouse ``metadata`` custom fields, Bundesagentur "
            "facets, Lever ``categories``, …). Keep small (~5kB "
            "serialized) — pre-strip large nested objects, raw HTML, "
            "etc. Serialized as a JSON string in CSV exports, native "
            "dict in parquet."
        ),
    )

    @model_validator(mode="after")
    def _populate_global_id(self) -> Self:
        """Compute ``global_id`` from ``ats_type`` + ``ats_id``.

        Runs after the rest of the model is validated so we can read
        the validated values. ``ats_id`` may be missing, empty, or
        contain control characters — in any of those cases we log an
        error and fall back to a UUID4 so the row still gets a unique
        identifier instead of failing the whole scrape.
        """
        normalized_id: str | None = None
        if self.ats_id is not None:
            stripped = self.ats_id.strip()
            if stripped and not _ATS_ID_FORBIDDEN_CHARS.search(stripped):
                normalized_id = stripped

        if normalized_id is None:
            log.error(
                "Job(ats_type=%s, url=%s) has missing/invalid ats_id=%r — "
                "falling back to UUID. Source scraper should be fixed.",
                self.ats_type.value,
                self.url,
                self.ats_id,
            )
            object.__setattr__(self, "global_id", str(uuid.uuid4()))
        else:
            # Persist the cleaned-up ats_id so downstream consumers
            # don't see the trailing-space or other whitespace forms.
            if normalized_id != self.ats_id:
                object.__setattr__(self, "ats_id", normalized_id)
            object.__setattr__(
                self, "global_id", f"{self.ats_type.value}:{normalized_id}"
            )
        return self

    @property
    def salary(self) -> Salary | None:
        if self.salary_currency is None:
            return None
        return Salary(
            currency=self.salary_currency,
            period=self.salary_period or "YEAR",
            min_amount=self.salary_min,
            max_amount=self.salary_max,
            summary=self.salary_summary,
        )
