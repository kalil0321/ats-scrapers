<p align="center">
  <img src="https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/assets/banner.jpeg" alt="ats-scrapers" />
</p>

# ats-scrapers

> **The open dataset and toolkit for global job market data.**
> 3.2M+ live jobs from 86 000+ companies, scraped directly from the ATS platforms where companies actually post. No LinkedIn, no reposts, no recruiters.

[![PyPI](https://img.shields.io/pypi/v/ats-scrapers.svg?color=brightgreen)](https://pypi.org/project/ats-scrapers/)
[![Python](https://img.shields.io/pypi/pyversions/ats-scrapers.svg?color=brightgreen)](https://pypi.org/project/ats-scrapers/)
[![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](LICENSE)

```python
from ats_scrapers import search

df = search(query="ml engineer", ats="greenhouse", location="Paris")
```

No API key, no auth, no rate limits. Install `ats-scrapers[parquet]` for
full-dataset search; the base install can query per-ATS CSV slices.

---

## Why ats-scrapers

Most job aggregators scrape LinkedIn and Indeed — both full of duplicates,
ghost listings, and reposts. **ats-scrapers goes one layer down**: directly to
the ATS platforms (Greenhouse, Lever, Ashby, Workday, BambooHR…) where
companies actually post.

- **Single source of truth** — every row comes from the company's own
  ATS, so titles, locations, and salaries are accurate.
- **No duplicates** — one ATS posting = one row.
- **Structured salary** when the ATS exposes it (Ashby, Greenhouse Pay
  Transparency, Lever salaryRange, etc.).
- **MIT licensed, fully open** — fork the dataset, fork the scrapers.

## Coverage

| Metric | Value |
|---|---:|
| Live jobs | **3 271 000+** |
| Companies | **86 000+** |
| ATS platforms | **47** |

Top 10 by job count:

| ATS | Jobs |
|---|---:|
| EURES (EU/EEA public-sector) | 1 498 440 |
| Workday | 449 167 |
| SmartRecruiters | 213 154 |
| SuccessFactors | 181 093 |
| Greenhouse | 169 812 |
| Oracle HCM | 144 106 |
| iCIMS | 120 934 |
| JazzHR | 71 050 |
| Lever | 68 303 |
| Phenom | 56 546 |

Counts come from the live manifest at
`https://storage.stapply.ai/jobhive/v1/manifest.json` — verify any time
with `ats-scrapers list-ats`.

## Install

```bash
pip install ats-scrapers
```

The import name is `ats_scrapers`. Up to 0.1.0 this project was
published as `jobhive-py` — that name is retired; install
`ats-scrapers` going forward.

Optional extras:

```bash
pip install "ats-scrapers[parquet]"     # faster downloads via Apache Parquet
pip install "ats-scrapers[scrapers]"    # build your own pipeline
pip install "ats-scrapers[all]"
```

## Two ways to use it

### 1. Query the public dataset

```python
from ats_scrapers import search

# Free-text title + location + remote filter
df = search(query="rust", ats="greenhouse", location="Berlin", remote=True)

# Restrict to one ATS slice (smaller download)
df = search(query="data engineer", ats="ashby")

# Full-dataset search needs the parquet extra because jobhive/v1/all is
# published as all.parquet.
#   pip install "ats-scrapers[parquet]"
df = search(query="ml engineer", location="Paris")

# Pandas all the way down
df.groupby("company").size().sort_values(ascending=False).head(20)
```

Every row carries:

```
global_id, url, title, company, ats_type, ats_id,
location, country_iso, region, is_remote, lat, lon,
salary_min, salary_max, salary_currency, salary_period, salary_summary,
employment_type, commitment, experience, department, team,
description, posted_at, fetched_at, language,
requisition_id, apply_url, raw
```

Full per-field semantics (types, defaults, derivation rules, examples)
live in [**`JOB_SCHEMA.md`**](./JOB_SCHEMA.md). `global_id` is the
cross-ATS unique key in the form `{ats_type}:{ats_id}`. Optional fields
are `None` when the source ATS doesn't expose them; `raw` keeps any
provider-specific fields the canonical schema doesn't represent.

### 2. Scrape your own companies

```python
from ats_scrapers.scrapers import GreenhouseScraper, LeverScraper, AshbyScraper

jobs = GreenhouseScraper("anthropic").fetch()    # → list[Job]
jobs = LeverScraper("palantir").fetch()
jobs = AshbyScraper("openai").fetch()
```

Or pick by name:

```python
from ats_scrapers.scrapers import get_scraper

scraper = get_scraper("ashby", "openai")
```

## Scrapers

**Multi-tenant ATS** (pass the company's slug on that ATS):

`Greenhouse`, `Lever`, `Ashby`, `SmartRecruiters`, `Workable`,
`Rippling`, `Personio`, `Gem`, `JoinCom`, `iCIMS`, `JazzHR`, `Breezy`,
`Teamtailor`, `Pinpoint`, `BambooHR`, `Cornerstone`, `Recruitee`,
`Recruiterbox`, `Eightfold`, `Avature`, `Phenom`, `Workday`, `Oracle`,
`SuccessFactors`, `Taleo`, `Mercor`.

**Custom big-tech APIs** (single-tenant, slug ignored): `Amazon`,
`Apple`, `Google`, `TikTok`, `Uber`.

**National public-sector aggregators**: `Bundesagentur` (DE),
`Arbetsformedlingen` (SE), `Eures` (EU/EEA-wide).

**Hybrid jobboards**: `WelcomeToTheJungle`.

**Browser-required** (run via [Browserbase](https://browserbase.com)
remote sessions): `Meta`, `Tesla`. Set `ATS_SCRAPERS_USE_BROWSERBASE=1`
together with `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` to
enable; without those env vars the scrapers log a warning and skip.
Tesla also needs a Browserbase project that bypasses Akamai (default
sessions are currently 403'd).

## CLI

```bash
ats-scrapers search "platform engineer" --location Paris --limit 20
ats-scrapers scrape ashby openai
ats-scrapers list-ats
```

## Contributing

**The goal is the largest open-source live job dataset on the
internet.** That's a forever project, and there's a clear path to make
it bigger:

- **Add a new ATS scraper** — every ATS we don't cover yet is a few
  thousand companies missing from the dataset. The scraper API is
  intentionally tiny: subclass `BaseScraper`, set `ats`, implement
  `fetch()`. See any file under `src/ats_scrapers/scrapers/` for a 50-line
  reference, and the `Job` model in `src/ats_scrapers/models.py` for the
  schema you populate.
- **Improve coverage on an existing ATS** — many scrapers extract
  description / salary / employment-type only when the ATS surfaces
  them. If you find a tenant where a field is structurally available
  but we're missing it, a one-line PR is welcome.
- **Add new tenants** — every supported ATS has a CSV under
  [`ats-companies/`](./ats-companies/). New rows = new companies in
  the dataset. One-line PRs are welcome.
- **Report broken scrapers** — open an issue with the slug and the
  failure mode. ATS APIs drift; flagging a regression early keeps the
  dataset accurate for everyone.

```bash
git clone https://github.com/stapply-ai/ats-scrapers
cd ats-scrapers
uv pip install -e ".[dev,scrapers]"
pytest
ruff check .
```

PRs welcome on `main`. CI is green for all 6 of {3.11, 3.12, 3.13} ×
{ubuntu, macos}; please keep it that way.

## License

MIT.

## Acknowledgments

Built with [Reverse API Engineer](https://github.com/kalil0321/reverse-api-engineer).
