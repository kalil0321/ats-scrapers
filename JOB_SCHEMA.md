# Job schema

Every row in the published `jobs.csv` / `jobs.parquet` files (and every
`Job` instance the scrapers produce) is one job posting in this shape.
Field names are part of the public contract — renames are a breaking
change.

This doc mirrors the [`Job` Pydantic model in
`src/jobhive/models.py`](src/jobhive/models.py). When the two drift,
the Pydantic descriptions are the source of truth; please update both.

---

## At a glance

| Group | Fields |
|---|---|
| **Identity** | `global_id`, `url`, `title`, `company`, `ats_type`, `ats_id` |
| **Location** | `location`, `country_iso`, `region`, `lat`, `lon`, `is_remote` |
| **Compensation** | `salary_currency`, `salary_period`, `salary_summary`, `salary_min`, `salary_max` |
| **Classification** | `experience`, `employment_type`, `department`, `team`, `requisition_id`, `apply_url`, `commitment` |
| **Content & timing** | `description`, `posted_at`, `fetched_at`, `language` |
| **Provider overflow** | `raw` |

29 columns total in the published CSV.

> **Heuristic vs LLM split.** The publisher's hardcoded inference is
> intentionally narrow: title-only `is_remote` (returns `True` or
> `None`, never `False`), tight-regex `salary_min`/`salary_max`
> parsing of `salary_summary`, and ATS-specific employment-type label
> maps. Anything that requires reading prose — figuring out a country
> from a free-form location string, parsing nuanced remote phrasing
> from a description — is left to the downstream LLM enrichment
> pipeline. Lots of these fields land as `None` when the ATS doesn't
> ship them structured; the LLM pass fills the gap.

---

## Identity

### `global_id` &nbsp;`str` &nbsp;*(derived)*

Globally unique identifier for the posting, formatted as
`{ats_type}:{ats_id}` when both are present.

- **Examples:** `ashby:engineer-2026`, `workday:R0136150`, `greenhouse:6849563`.
- **Separator:** `:`. Parsers should split on the **first** colon —
  `ats_id` may itself contain colons (some Taleo URLs encode multiple).
  Example: `"taleo:acme:req:12345"` parses as
  `("taleo", "acme:req:12345")`.
- **Fallback:** when `ats_id` is missing, empty after whitespace strip,
  or contains control characters (`\n` / `\t` / `\0`), `global_id`
  becomes a fresh UUID4 string and the offending row is logged with an
  ERROR. This keeps the scrape moving instead of crashing on bad data;
  the responsible scraper still gets flagged in the logs.
- **Don't pass it manually.** A `model_validator` computes it from
  `ats_type` + `ats_id`, overwriting any value you supplied.
- **Case is preserved.** Workday's `R0136150` ≠ `r0136150` — collapsing
  case would risk merging legitimately distinct postings.

### `url` &nbsp;`HttpUrl` &nbsp;*(required)*

Public posting URL on the ATS. Always present. The primary stable
identifier consumers should use to deduplicate or link to the live
page.

### `title` &nbsp;`str` &nbsp;*(required)*

Free-form job title as posted. Examples: `Senior Software Engineer,
Reality Labs`, `Engenheiro QA Python`. May contain spaces, punctuation,
or non-ASCII characters.

### `company` &nbsp;`str` &nbsp;*(required)*

Display name of the hiring employer. **Distinct from `ats_id`:** the
same company can have `company="OpenAI"` and `ats_id="openai"` on Ashby.

Per-ATS conventions vary — Greenhouse stores a numeric board id,
Workday the human-readable name, Oracle the API host, etc. — so don't
join on `company` cross-ATS. Use `ats_type` + `ats_id` for cross-ATS
keys and `requisition_id` (when both rows have it) for cross-ATS
matching.

### `ats_type` &nbsp;`ATSType` &nbsp;*(required)*

Which ATS platform serves this posting. Determines which scraper
produced the row and what shape `ats_id` takes. See the `ATSType` enum
in `src/jobhive/models.py` for the full list (~50 values).

### `ats_id` &nbsp;`str | None` &nbsp;*(optional, defensive)*

Per-ATS identifier. Unique within `ats_type` but not globally — use
`global_id` for that. Form depends on the ATS:

| ATS | Typical `ats_id` |
|---|---|
| Greenhouse | numeric: `"6849563"` |
| Workday | mixed-case: `"R0136150"` |
| Lever | UUID: `"62a8c1f4-..."` |
| iCIMS | dash-separated: `"job-2026-04-eng"` |
| Bundesagentur | hex hash |
| Apple | numeric (occasionally trail-spaced) |

Optional only as a defensive type — every scraper should set it. When
null, empty, or malformed, `global_id` falls back to a UUID4 and an
ERROR is logged.

---

## Location

### `location` &nbsp;`str | None`

Free-form location string as posted. Examples: `Paris, France`,
`Remote — US`, `Berlin or Remote`. Multi-location postings are rendered
as comma-joined when the ATS exposes a list.

### `country_iso` &nbsp;`str | None`

ISO 3166-1 alpha-2 country code, always uppercase 2 letters: `US`,
`FR`, `DE`, `BR`, `JP`, `IN`. Set by the scraper when the source ATS
exposes a structured country (Bundesagentur, EURES, SuccessFactors).
Otherwise `None` — the downstream LLM enrichment pass derives it from
`location` text.

### `region` &nbsp;`str | None`

Continent the role lives on, when known. One of:

- `Europe`
- `North America`
- `South America`
- `Asia`
- `Africa`
- `Oceania`
- `Antarctica` (theoretical)

Coarser than `country_iso` so consumers can group EMEA / APAC without
juggling country lists. Sub-national entities (US states, German
Bundesländer, Indian states) are *not* stored here — they live in
`location` text.

### `lat`, `lon` &nbsp;`float | None`

WGS-84 geocoded coordinates when the ATS provides them (rare — most
don't). Not derived from `location` text. A future geocoding service
is expected to fill these for rows where the scraper leaves them
`None`.

### `is_remote` &nbsp;`bool | None`

Whether the role can be performed remotely.

- Set by the scraper when the ATS exposes a flag (e.g. Lever's
  `workplaceType`).
- Otherwise narrowly inferred from the **title** at publish time by
  `jobhive.enrichment.infer_is_remote` — that heuristic only ever
  returns `True` (never `False`), since the absence of a remote
  keyword in the title is **not** evidence the role is on-site.
- `None` means we genuinely don't know. LLM-based enrichment
  downstream is expected to fill the rest from the full posting
  context.

---

## Compensation

Salary fields work together: `salary_currency` + `salary_period` are
metadata, `salary_min`/`salary_max` are the structured numeric range,
`salary_summary` is the original string the ATS displays. When the ATS
ships only the summary string, `salary_min`/`salary_max` are derived
via `jobhive.enrichment.parse_salary_range` at publish time.

### `salary_currency` &nbsp;`str | None`

ISO 4217 currency code (`USD`, `EUR`, `GBP`, `BRL`, …). `None` when no
salary is exposed OR when the salary is present only as untyped free
text.

### `salary_period` &nbsp;`Literal["HOUR", "DAY", "WEEK", "MONTH", "YEAR"] | None`

Period the salary applies to. `YEAR` is the most common; `HOUR` shows
up on hourly/contractor postings.

### `salary_summary` &nbsp;`str | None`

Original salary string as the ATS displays it.

- `"$120K – $160K"`
- `"45.000 € / Jahr"`
- `"up to £80k"`
- `"R$3.000 - R$5.000"`

### `salary_min`, `salary_max` &nbsp;`float | None`

Lower / upper bounds in `salary_currency`. Either set directly by the
scraper from a structured ATS field, or derived from `salary_summary`.

---

## Classification

### `experience` &nbsp;`int | None`

Required years of experience as an integer when the ATS exposes a
structured value. `None` when missing or only described in prose
("3+ years", "Senior").

### `employment_type` &nbsp;`Literal["FULL_TIME", "PART_TIME", "CONTRACT", "INTERN", "TEMPORARY"] | None`

**Normalized** employment type. Cross-ATS comparable — use this for
filtering. The ATS-specific raw label lives in `commitment`.

### `department`, `team` &nbsp;`str | None`

`department` is the high-level org grouping (`Engineering`, `Sales`,
`Marketing`, …). `team` is the finer-grained sub-team / squad
(`Reality Labs`, `Payments Infra`, …). Often `team` is empty even when
`department` is set.

### `requisition_id` &nbsp;`str | None`

**Employer-internal** requisition identifier (Greenhouse
`requisition_id`, Workday `bulletFields[0]`, Lever's private id,
Bundesagentur `hashId`).

Distinct from `ats_id` which is **platform-side**. The same role
mirrored on two different ATSes shares the same `requisition_id` but
has two different `ats_id` — strong cross-ATS dedup signal:

```
Greenhouse @ Anthropic   ats_id="6849563"        requisition_id="ENG-2026-184"
Eightfold (same job)     ats_id="62a8c1f4..."    requisition_id="ENG-2026-184"  ← same
```

### `apply_url` &nbsp;`HttpUrl | None`

Direct application URL when **distinct from** the posting `url`. Some
ATSes redirect to a separate apply destination — Workable's widget,
Bundesagentur's external boards, YC's `workatastartup.com`.

### `commitment` &nbsp;`str | None`

Free-form commitment label as posted by the ATS. Distinct from
`employment_type` which is the normalized enum:

| Source | `commitment` | `employment_type` |
|---|---|---|
| Lever (FR) | `"CDI"` | `"FULL_TIME"` |
| Lever (EN) | `"Full-time, 40h"` | `"FULL_TIME"` |
| Bundesagentur | `"Vollzeit, 38 h/Woche"` | `"FULL_TIME"` |
| Arbetsförmedlingen | `"Heltid"` | `"FULL_TIME"` |
| Workable | `"Contractor — 6 mois"` | `"CONTRACT"` |
| Mercor | `"32h/week"` | `null` (no clean type) |

Use `commitment` to preserve language, hours, and contract granularity
the enum loses.

---

## Content & timing

### `description` &nbsp;`str | None`

Plain-text job description. HTML and Markdown are stripped to text.
Truncated to ~10 kB when the source exceeds it.

### `posted_at` &nbsp;`datetime | None`

When the ATS reports the posting was first published. UTC. `None` when
the ATS doesn't expose this — common on aggregator sites and some
legacy ATSes.

### `fetched_at` &nbsp;`datetime | None`

When jobhive last saw this posting. UTC.

### `language` &nbsp;`str | None`

ISO 639-1 lowercase 2-letter code for the language of the **listing
itself**: `en`, `fr`, `de`, `pt`, `es`, `ja`, … Set by the scraper
when the source ATS exposes a locale (Lever's locale path,
Bundesagentur's `sprache`, EURES `language`, Welcome to the Jungle's
locale prefix). Otherwise `None` and LLM enrichment downstream fills
it from `title` / `description`.

Distinct from any "required language" the role itself might want for
the work — that lives in `description` and is out of scope for the
canonical schema.

---

## Provider-specific overflow

### `raw` &nbsp;`dict[str, object] | None`

ATS-specific fields the canonical schema can't represent — kept
verbatim. Examples:

| ATS | Typical `raw` keys |
|---|---|
| Greenhouse | `metadata` (custom fields) |
| Bundesagentur | `arbeitszeit`, `branche`, `befristung` |
| Lever | `categories`, `tags` |
| Programathor | `skills`, `company_type`, `salary_text` |
| Personio | `subcompany`, `office`, `occupationCategory` |

Keep small (~5 kB serialized). Pre-strip large nested objects, raw
HTML, full job descriptions (those go in `description`). Serialized as
a JSON string in CSV exports, native dict in parquet.

---

## Frequently confused

**`ats_id` vs `requisition_id` vs `global_id`** —
- `ats_id` is the ATS-platform's internal id (different per platform).
- `requisition_id` is the *employer's* internal id (same across
  platforms when one job is mirrored on multiple ATSes).
- `global_id` is jobhive's `{ats_type}:{ats_id}` composite — the unique
  identifier for the row in the dataset.

**`employment_type` vs `commitment`** —
- `employment_type` is a 5-value enum (full-time / part-time /
  contract / intern / temporary). Filter on this.
- `commitment` is the raw ATS string (`"CDI"`, `"Heltid"`,
  `"32h/week"`). Display this.

**`is_remote` and `salary_min/max` are sometimes derived** —
the publisher fills them from `title` text (narrow, title-only) and
`salary_summary` when the ATS doesn't ship them structured. The CSV
/ parquet doesn't distinguish derived from source-provided values;
if you need to tell them apart, look at the raw ATS payload via
`raw`.
