"""Core data models for jobs, companies, and salary information.

These models are the canonical schema across every ATS scraper and the
public dataset on storage.stapply.ai. Adding a field here means: the
dataset gets a new column, every scraper must populate it (or leave
it None), and the parquet schema gets a new field.

The Job schema is also documented for human readers in
``docs/JOB_SCHEMA.md`` — keep the two in sync. ``Field``
descriptions in this file are the source of truth; the markdown is a
view onto them.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    HttpUrl,
    field_validator,
    model_validator,
)

log = logging.getLogger(__name__)


class ATSType(StrEnum):
    """Known job-source identifiers.

    Most values identify an ATS or job source with a registered scraper.
    ``CUSTOM`` is reserved for user-defined sources. Hosted-manifest keys are
    strings so newly published sources do not break older clients.
    """

    ADP = "adp"
    APPLICANTPRO = "applicantpro"
    APPLITRACK = "applitrack"
    ASHBY = "ashby"
    AVATURE = "avature"
    BEISEN = "beisen"
    BEISEN_LEGACY = "beisen_legacy"
    # CATS careers portals ({tenant}.catsone.com).
    CATSONE = "catsone"
    # ClearCompany hosted career sites (apply.clearcompany.com/{site}).
    CLEARCOMPANY = "clearcompany"
    CORNERSTONE = "cornerstone"
    DARWINBOX = "darwinbox"
    DAYFORCE = "dayforce"
    EIGHTFOLD = "eightfold"
    FEISHU_HIRE = "feishu_hire"
    GEM = "gem"
    GREENHOUSE = "greenhouse"
    GREETING = "greeting"
    GUPY = "gupy"
    HERP = "herp"
    HIREOLOGY = "hireology"
    HRMOS = "hrmos"
    ICIMS = "icims"
    # iCIMS career sites (formerly Jibe) on the employer's own domain.
    JIBE = "jibe"
    JOBVITE = "jobvite"
    JOIN_COM = "join_com"
    KEKA = "keka"
    LEVER = "lever"
    MANATAL = "manatal"
    MERCOR = "mercor"
    MOKA = "moka"
    NINEHIRE = "ninehire"
    ORACLE = "oracle"
    PAGEUP = "pageup"
    PAYCOM = "paycom"
    PAYLOCITY = "paylocity"
    # PeopleAdmin applicant portals ({tenant}.peopleadmin.com).
    PEOPLEADMIN = "peopleadmin"
    PERSONIO = "personio"
    PHENOM = "phenom"
    PINPOINT = "pinpoint"
    RECRUITERBOX = "recruiterbox"
    RIPPLING = "rippling"
    SMARTRECRUITERS = "smartrecruiters"
    SOFTGARDEN = "softgarden"
    SUCCESSFACTORS = "successfactors"
    TALENTIO = "talentio"
    UKG = "ukg"
    VARBI = "varbi"
    WINTALENT = "wintalent"
    WORKABLE = "workable"
    # Workable's public job marketplace: every customer's live postings
    # in one paginated feed (single-source scraper).
    WORKABLE_MARKETPLACE = "workable_marketplace"
    WORKDAY = "workday"
    ZOHO_RECRUIT = "zoho_recruit"
    # Big-tech custom careers systems (single-tenant, bespoke APIs)
    AMAZON = "amazon"
    APPLE = "apple"
    BYTEDANCE = "bytedance"
    GOOGLE = "google"
    JD = "jd"
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
    # Wantedly (Japan): employers publish their openings on their company page.
    WANTEDLY = "wantedly"
    REMOTEOK = "remoteok"
    WEWORKREMOTELY = "weworkremotely"
    PROGRAMATHOR = "programathor"
    BUILTIN = "builtin"
    JOBBERMAN = "jobberman"
    BRIGHTERMONDAY = "brightermonday"
    EMPLOITIC = "emploitic"
    JOBSCH = "jobsch"
    JOBSCZ = "jobs_cz"
    MANFRED = "manfred"
    THEHUB = "thehub"
    YCOMBINATOR = "ycombinator"
    WELLFOUND = "wellfound"
    INFOJOBSES = "infojobs_es"
    JOBBANKCA = "jobbankca"
    MYCAREERSFUTURE = "mycareersfuture"
    SEEK = "seek"
    # Additional multi-tenant ATSes (post-0.1)
    BAMBOOHR = "bamboohr"
    BREEZY = "breezy"
    JAZZHR = "jazzhr"
    RECRUITEE = "recruitee"
    TALEO = "taleo"
    # Oracle Taleo Enterprise Edition career sections ({zone}.taleo.net);
    # "taleo" is the Business Edition (*.tbe.taleo.net).
    TALEO_ENTERPRISE = "taleo_enterprise"
    # Cegid Talentsoft career sites ({site}.talent-soft.com).
    TALENTSOFT = "talentsoft"
    # Radancy TalentBrew career sites on the employer's own domain.
    TALENTBREW = "talentbrew"
    TEAMTAILOR = "teamtailor"
    CUSTOM = "custom"


SalaryPeriod = Literal["HOUR", "DAY", "WEEK", "MONTH", "YEAR"]

_COMPENSATION_PERIODS: dict[str, SalaryPeriod] = {
    "HOUR": "HOUR", "HOURLY": "HOUR", "DAY": "DAY", "DAILY": "DAY",
    "WEEK": "WEEK", "WEEKLY": "WEEK", "MONTH": "MONTH", "MONTHLY": "MONTH",
    "YEAR": "YEAR", "YEARLY": "YEAR", "ANNUAL": "YEAR", "ANNUALLY": "YEAR",
}


def normalize_compensation_period(value: object) -> SalaryPeriod | None:
    """Map single-period pay rates such as '1 YEAR' to a salary period."""
    if not isinstance(value, str):
        return None
    token = value.strip().upper()
    return _COMPENSATION_PERIODS.get(re.sub(r"^(?:1|PER)\s+", "", token))


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
    """A company tracked by ats-scrapers."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(..., description="ATS-specific identifier (e.g. 'openai' on Ashby)")
    name: str
    ats: ATSType
    careers_url: HttpUrl | None = None
    website: HttpUrl | None = None


class CompensationComponent(BaseModel):
    """One source-reported pay component in one compensation tier."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["base_salary", "bonus", "commission", "equity", "allowance", "other"]
    source_label: str | None = None
    unit: Literal["money", "percent", "shares"] | None = None
    currency: str | None = None
    period: SalaryPeriod | None = None
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    tier: str | None = Field(default=None, description="Source name of the applicable tier or package.")
    summary: str | None = None


class Occupation(BaseModel):
    """Primary occupational classification assigned by the source."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    code: str | None = None
    taxonomy: str | None = Field(default=None, description="Source vocabulary or published classification system.")


RequirementLevel = Literal["required", "preferred", "optional", "unspecified"]


class _Requirement(BaseModel):
    """Original wording and the priority explicitly indicated by the source."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    label: str = Field(min_length=1)
    requirement: RequirementLevel = "unspecified"


class EducationRequirement(_Requirement):
    """An educational qualification attached to the posting."""


class SkillRequirement(_Requirement):
    """A skill attached to the posting."""


class JobLocation(BaseModel):
    """One advertised place of work as identified by the source."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    label: str = Field(min_length=1)
    city: str | None = None
    region: str | None = Field(default=None, description="Administrative area of this location.")
    country_iso: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    is_primary: bool | None = Field(default=None, strict=True)


WorkplaceType = Literal["onsite", "hybrid", "remote", "onsite_or_remote"]
WorkloadPercentage = Annotated[float, Field(strict=True, ge=0, le=100)]


class WorkloadPercentages(BaseModel):
    """Advertised percentages as discrete values or a range with inclusive bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: list[WorkloadPercentage] | None = Field(default=None, min_length=1)
    minimum: WorkloadPercentage | None = None
    maximum: WorkloadPercentage | None = None

    @model_validator(mode="after")
    def _check_representation(self) -> Self:
        has_bounds = self.minimum is not None or self.maximum is not None
        if (self.values is not None) == has_bounds:
            raise ValueError("Supply discrete values or a minimum or maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum workload exceeds maximum workload")
        return self


EmploymentType = Literal["FULL_TIME", "PART_TIME", "CONTRACT", "INTERN", "TEMPORARY"]
EngagementType = Literal[
    "permanent", "fixed_term", "independent_contractor", "temporary", "apprenticeship", "internship",
]
SourceTimestamp = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]


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

    The fields fall into six groups, listed in the order they appear
    below:

    1. **Identity** (``global_id``, ``url``, ``title``, ``company``,
       ``ats_type``, ``ats_id``). What the row *is*.

    2. **Location** (``location``, ``country_iso``, ``region``,
       ``lat``, ``lon``, ``is_remote``, ``locations``, ``workplace_type``). Where the role lives.
       ``is_remote`` is *narrowly* inferred from the ``title`` by
       ``ats_scrapers.enrichment.infer_is_remote`` when the ATS doesn't
       surface a flag — it only ever returns ``True`` (never
       ``False``) so the absence of a marker doesn't get mis-classified
       as on-site. ``country_iso`` / ``region`` / ``lat`` / ``lon``
       are scraper-set when the source exposes them, otherwise filled
       by the downstream LLM enrichment pass / a geocoding service.

    3. **Compensation** (``salary_currency``, ``salary_period``,
       ``salary_summary``, ``salary_min``, ``salary_max``, ``compensation_components``).
       ``salary_min`` / ``salary_max`` are *derived* from
       ``salary_summary`` via ``ats_scrapers.enrichment.parse_salary_range``
       when the ATS exposes only free text.

    4. **Classification** (``experience``, ``employment_type``,
       ``department``, ``team``, ``requisition_id``, ``apply_url``,
       ``commitment``, ``workload_percentages``, ``occupation``,
       ``engagement_type``, ``education_requirements``, ``skill_requirements``). Optional — set when the source API exposes
       them, ``None`` otherwise.

    5. **Content & timing** (``description``, ``posted_at``,
       ``source_created_at``, ``source_updated_at``, ``fetched_at``,
       ``application_deadline``, ``language``).
       ``application_deadline`` is an explicit deadline the ATS exposes
       (``None`` when unavailable, never inferred from other timestamps).
       ``language`` is the listing's locale code (ISO 639-1, e.g.
       ``en`` / ``fr``).

    6. **Provider-specific overflow** (``raw``): a JSON dict captured
       at scrape-time so we don't lose ATS-specific fields the
       canonical schema can't represent (Greenhouse ``metadata``
       custom fields, Bundesagentur ``arbeitszeit``/``branche``,
       Lever ``categories.*``, etc.).

    Heuristic-vs-LLM split: anything that requires reading prose
    (description text, location strings to derive country, …) is
    intentionally left to the downstream LLM enrichment pipeline.
    Hardcoded inference here is restricted to (a) cheap robust
    look-ups (employment-type label maps), (b) tight regex parsing
    on conventionally-structured text (``salary_summary``), and
    (c) a single title-only ``is_remote`` keyword check that only
    ever asserts ``True``.
    """

    model_config = ConfigDict(populate_by_name=True)

    # --- Identity ---------------------------------------------------------

    global_id: str = Field(
        default="",
        description=(
            "Globally unique identifier for the posting, formatted as "
            "``{ats_type}:{ats_id}`` when both are set (e.g. "
            "``ashby:engineer-2026`` or "
            "``workday:acme.wd1.myworkdayjobs.com/job/USA/Engineer_R-1``). The "
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
            "Display name of the hiring employer. Distinct from ``ats_id``, "
            "which identifies the posting within its source (for example, "
            "an Ashby UUID or Greenhouse numeric job id)."
        ),
    )
    ats_type: ATSType = Field(
        ...,
        alias="ats_type",
        description=(
            "Which ATS platform or job source serves this posting. "
            "Determines the adapter that produced the row and the format of "
            "``ats_id``."
        ),
    )
    ats_id: str | None = Field(
        default=None,
        description=(
            "Per-ATS identifier for the posting — Greenhouse numeric "
            "id, Workday tenant host and posting path, Lever UUID, etc. Unique "
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
            "``Remote — US``, ``Berlin or Remote``). When the ATS exposes "
            "a list, a multi-location posting keeps every place, joined "
            "with ``; `` by most sources (`` | `` or ``, `` by a few); a "
            "single place often holds commas itself."
        ),
    )
    country_iso: str | None = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 country code (``US``, ``FR``, ``DE``, "
            "``BR``, …). Set by the scraper when the source ATS "
            "exposes a structured country (Bundesagentur, EURES, "
            "SuccessFactors). Otherwise ``None`` and the LLM "
            "enrichment pass downstream is expected to derive it from "
            "``location`` text. Always uppercase 2 letters."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "Continent the role lives on, when known: ``Europe``, "
            "``North America``, ``Asia``, ``South America``, "
            "``Africa``, ``Oceania``, or ``Antarctica`` (last one is "
            "theoretical). For remote roles the value depends on the "
            "stated remote zone — ``None`` when unspecified. Coarser "
            "than ``country_iso`` so consumers can group EMEA / APAC "
            "without juggling country lists. Sub-national entities "
            "(US states, German Bundesländer, …) live in "
            "``location`` instead — keep this field at the continent "
            "level."
        ),
    )
    lat: float | None = Field(
        default=None,
        description=(
            "Latitude in WGS-84 degrees when the ATS provides "
            "geocoded coordinates (rare — most don't). Not derived "
            "from ``location`` text. A future geocoding service is "
            "expected to fill this for rows where the scraper leaves "
            "it ``None``."
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
            "scraper when the ATS exposes a flag. Otherwise inferred "
            "from the **title** by ``ats_scrapers.enrichment.infer_is_remote`` "
            "at publish time — that heuristic only ever returns "
            "``True`` (never ``False``) since the absence of a remote "
            "marker in the title is not evidence of on-site. ``None`` "
            "means we genuinely don't know; LLM enrichment downstream "
            "is expected to fill the rest."
        ),
    )

    workplace_type: WorkplaceType | None = Field(default=None, description="Source-stated attendance arrangement.")
    locations: list[JobLocation] | None = Field(default=None, description="Advertised places of work in source order.")

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
            "``ats_scrapers.enrichment.parse_salary_range`` at publish time."
        ),
    )
    salary_max: float | None = Field(
        default=None,
        description=(
            "Upper bound of the salary range, in ``salary_currency``. "
            "Same population logic as ``salary_min``."
        ),
    )

    compensation_components: list[CompensationComponent] | None = Field(
        default=None, description="Source pay components, with their original unit, bounds and applicable tier.",
    )

    # --- Classification ---------------------------------------------------

    workload_percentages: WorkloadPercentages | None = Field(
        default=None, description="Explicit percentages of a full-time role, given individually or as a range.",
    )
    occupation: Occupation | None = Field(
        default=None, description="Primary occupation, job family or function classified by the source.",
    )
    engagement_type: EngagementType | None = Field(
        default=None, description="Explicit contractual relationship or duration, independent of working hours.",
    )
    education_requirements: list[EducationRequirement] | None = Field(
        default=None, description="Educational qualifications the source attaches to the job and their stated status.",
    )
    skill_requirements: list[SkillRequirement] | None = Field(
        default=None, description="Source-listed skills and their stated priority.",
    )
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
            "``requisition_id``, a Workday bullet field embedded in the "
            "posting path, Lever's "
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
            "stripped to text. Truncated to ~25k chars when the source "
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
    source_created_at: SourceTimestamp | None = Field(
        default=None, description="UTC time at which the source says this posting record was created.",
    )
    source_updated_at: SourceTimestamp | None = Field(
        default=None, description="UTC time at which the source says this posting record was last edited.",
    )
    fetched_at: datetime | None = Field(
        default=None,
        description="When ats-scrapers last saw this posting (UTC).",
    )
    application_deadline: datetime | None = Field(
        default=None,
        description=(
            "Source-provided date after which the posting should no longer be "
            "treated as open (e.g. "
            "SuccessFactors ``g:expiration_date`` in the Google Merchant "
            "namespace, schema.org JobPosting ``validThrough``). This may be "
            "an employer-stated application deadline or a platform-generated "
            "validity horizon. ``None`` when unavailable — never inferred "
            "locally from ``posted_at`` or ``fetched_at``."
        ),
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 lowercase 2-letter code for the language of the "
            "**listing itself** (``en``, ``fr``, ``de``, ``pt``, ``es``, "
            "``ja``, …). Set by the scraper when the source ATS exposes "
            "a locale (Lever, Bundesagentur, EURES, Welcome to the "
            "Jungle). Otherwise ``None`` and LLM enrichment downstream "
            "fills it from ``title`` / ``description``. Distinct from "
            "any 'required language' the role itself might want — that "
            "lives in ``description`` and is out of scope for the "
            "canonical schema."
        ),
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

    @field_validator("workload_percentages", mode="before")
    @classmethod
    def _read_workload_list(cls, value: object) -> object:
        if isinstance(value, list):
            return {"values": value} if value else None
        return value

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
