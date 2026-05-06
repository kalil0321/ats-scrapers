"""Core data models for jobs, companies, and salary information.

These models are the canonical schema across every ATS scraper and the public
dataset on storage.stapply.ai. Adding a field here means: the dataset gets a
new column, every scraper must populate it (or leave it None), and the parquet
schema gets a new field.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ATSType(StrEnum):
    """Supported applicant tracking systems.

    A company belongs to exactly one ATS. The ATS determines which scraper
    knows how to fetch its jobs and how the careers page is structured.
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

    Stored separately from `Job` so the same shape can be reused for total comp,
    base, equity, etc. — currently only base is populated.
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
Seniority = Literal["INTERN", "ENTRY", "MID", "SENIOR", "STAFF", "PRINCIPAL", "DIRECTOR", "EXECUTIVE"]


class Job(BaseModel):
    """A job posting — the canonical row across the entire dataset.

    Every scraper produces `Job` instances; the public CSV/Parquet exports use
    these field names verbatim. Backwards compatibility on field names is part
    of the public contract.

    Fields fall in three tiers:

    - **Cross-ATS canonical**: ``url``/``title``/``company``/``ats_type``/
      ``ats_id``/``location``/``posted_at``. Every scraper sets these.
    - **Common-but-optional**: salary, employment type, seniority, etc.
      Set when the source API exposes them; ``None`` otherwise. The
      enrichment passes (``infer_*``) populate ``is_remote`` / ``seniority``
      from heuristics when missing.
    - **Provider-specific overflow** (``raw``): a JSON dict captured at
      scrape-time so we don't lose ATS-specific fields the canonical
      schema can't represent (Greenhouse ``metadata`` custom fields,
      Bundesagentur ``arbeitszeit``/``branche``, Lever ``categories.*``,
      etc.). Stored as a JSON string in CSV / dict in parquet.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: HttpUrl
    title: str
    company: str
    ats_type: ATSType = Field(..., alias="ats_type")
    ats_id: str

    location: str | None = None
    lat: float | None = None
    lon: float | None = None
    is_remote: bool | None = None

    salary_currency: str | None = None
    salary_period: SalaryPeriod | None = None
    salary_summary: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None

    experience: int | None = Field(None, description="Required years of experience")
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    department: str | None = None
    team: str | None = None

    # Tier 2 additions (2026-05): pinning a strong cross-ATS dedup signal
    # and the apply destination, both surfaced by most ATS APIs.
    requisition_id: str | None = Field(
        None,
        description=(
            "Employer-internal requisition identifier (Greenhouse "
            "``requisition_id``, Workday ``bulletFields[0]``, Lever's "
            "private id, Bundesagentur ``hashId``). Same value across "
            "ATSes for the same role — strong dedup signal."
        ),
    )
    apply_url: HttpUrl | None = Field(
        None,
        description=(
            "Direct application URL when distinct from the posting URL. "
            "Some ATSes (Workable widget, Bundesagentur external boards) "
            "redirect to a separate apply destination."
        ),
    )
    commitment: str | None = Field(
        None,
        description=(
            "Free-form commitment label from the source ATS (Lever's "
            "``commitment``, Workable's ``type``, Bundesagentur's "
            "``arbeitszeit`` description). Distinct from ``employment_type`` "
            "which is the normalized enum."
        ),
    )

    description: str | None = Field(
        None, description="Plain-text description; truncated to ~10kB if longer"
    )

    posted_at: datetime | None = None
    fetched_at: datetime | None = Field(None, description="When jobhive last saw this posting")

    # Tier 3: ATS-specific overflow. Anything in the source payload that
    # doesn't map cleanly to a canonical field can be stashed here so we
    # don't lose it. Keep it small (~5kB serialized) — pre-strip large
    # nested objects, raw HTML, etc.
    raw: dict[str, object] | None = Field(
        default=None,
        description=(
            "Provider-specific overflow fields kept verbatim "
            "(Greenhouse metadata custom fields, Bundesagentur facets, "
            "Lever categories, etc.). Serialized as JSON in CSV exports."
        ),
    )

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
