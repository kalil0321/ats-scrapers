# Changelog

All notable changes to **ats-scrapers** (formerly **jobhive**) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-07-22

### Changed — project renamed to `ats-scrapers`

The project is now published on PyPI as **`ats-scrapers`** with import
name **`ats_scrapers`**, matching the GitHub repository. The old
`jobhive-py` distribution is retired; its final release points here.

Breaking (mechanical) changes — a find-and-replace upgrade:

- `import jobhive` → `import ats_scrapers` (all submodules keep their
  paths: `ats_scrapers.scrapers`, `ats_scrapers.client`, …).
- Base exception `JobHiveError` → `ATSScrapersError`.
- Environment variables `JOBHIVE_*` → `ATS_SCRAPERS_*`
  (e.g. `ATS_SCRAPERS_USE_BROWSERBASE`).
- CLI entry point `jobhive` → `ats-scrapers`.

Not changed: the `Job`/`Company` schema, all scraper class names, and
the published dataset layout (`storage.stapply.ai/jobhive/v1/…` URLs
remain valid — the storage prefix is independent of the package name).

## [0.1.0] — 2026-05-08

Initial public release.

Distributed on PyPI as **`jobhive-py`** (the `jobhive` name was too
similar to an existing project for PyPI's name policy). The import
name is still `jobhive` — `pip install jobhive-py` then
`from jobhive import search`.

### Dataset

- 3.3M+ live jobs from 400 000+ companies, refreshed every 24 hours.
- Hosted manifest at `https://storage.stapply.ai/jobhive/v1/manifest.json`,
  served as Parquet/CSV via Cloudflare R2.

### Library

- `jobhive.search(query, location, remote, salary_min, ats)` — query the
  public dataset directly, no auth, no rate limits.
- `jobhive.Manifest` — programmatic access to the live manifest
  (per-ATS counts, last-refresh timestamps).
- Canonical `Job` and `Company` Pydantic models, with `ATSType`,
  `EmploymentType`, `Salary`, and `SalaryPeriod` re-exported at the
  package root.
- `py.typed` marker shipped — fully typed public API.

### Scrapers (50)

Multi-tenant ATS:
`Greenhouse`, `Lever`, `Ashby`, `SmartRecruiters`, `Workable`,
`Rippling`, `Personio`, `Gem`, `JoinCom`, `iCIMS`, `JazzHR`, `Breezy`,
`Teamtailor`, `Pinpoint`, `BambooHR`, `Cornerstone`, `Recruitee`,
`Recruiterbox`, `Eightfold`, `Avature`, `Phenom`, `Workday`, `Oracle`,
`SuccessFactors`, `Taleo`, `Mercor`.

Custom big-tech APIs (single-tenant): `Amazon`, `Apple`, `Google`,
`TikTok`, `Uber`.

Public-sector aggregators: `Bundesagentur` (DE), `Arbetsformedlingen`
(SE), `Eures` (EU/EEA-wide), `USAJobs` (US).

Direct-posting jobboards: `BuiltIn`, `GetOnBrd`, `JobsCh`, `Manfred`,
`Programathor`, `RemoteOK`, `TheHub`, `TheMuse`, `Wanted`, `Wellfound`,
`WeWorkRemotely`, `YCombinator`, `WelcomeToTheJungle`.

Browser-required (placeholders): `Tesla`, `Meta` — surface as
`ATSType` members but raise on `fetch()` until the optional browser
backend ships in 0.2.

### CLI

- `jobhive search` — query the dataset from the terminal.
- `jobhive scrape <ats> <slug>` — run a single scraper.
- `jobhive list-ats` — show every supported ATS and live row counts.

### Optional extras

- `jobhive-py[parquet]` — faster manifest downloads via Apache Parquet.
- `jobhive-py[scrapers]` — enable BYO-pipeline scraping
  (`aiohttp`, `beautifulsoup4`, `html2text`).
- `jobhive-py[publish]` — Cloudflare R2 + Parquet publishing toolchain.
- `jobhive-py[discovery]` — Firecrawl-based tenant discovery.
- `jobhive-py[all]` — everything above.

### Notes

- Avature: ~54 of 88 known tenants 406-block direct HTTP at the load
  balancer. The scraper transparently falls back to a Browserbase /
  Playwright session when `BROWSERBASE_API_KEY` is configured;
  otherwise those tenants are skipped with a warning.
- Bundesagentur: contract-break failures (401 / 404 / malformed JSON)
  now crash the scrape — only transient WAF-class failures are
  swallowed and logged.
- Workday "N Locations" rollups are resolved per-job via the detail
  endpoint so multi-office postings produce one row per location.

[0.1.0]: https://github.com/stapply-ai/ats-scrapers/releases/tag/v0.1.0
