# Changelog

All notable changes to **ats-scrapers** are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-09-02

### Added

- 19 installable source adapters: ADP Workforce Now, Beisen, Beisen Legacy,
  ByteDance, Darwinbox, Dayforce, Gupy, HERP, HRMOS, Job Bank Canada, Jobvite,
  Keka, Moka, PageUp, Paycom, Paylocity, SEEK, Softgarden, and UKG Pro.
- Careers-URL resolution for the newly supported multi-tenant platforms.
- `Job.application_deadline` for explicit source-provided deadlines, currently
  populated from `g:expiration_date` in SuccessFactors RSS feeds.
- Workday posting start and end dates in `Job.raw` when supplied by the detail
  endpoint.

### Changed

- SuccessFactors supports legacy XML feeds in addition to the existing API.
- Bundesagentur uses the v6 API with expanded profession coverage, bounded
  retries, and fail-closed handling for incomplete responses.
- Dayforce supports multiple public-feed variants used by different tenants.
- Moka scraping installs AES-CBC support through the `scrapers` extra.
- The package description no longer embeds dataset counts that become stale
  between releases.

### Fixed

- ByteDance retries a full catalogue snapshot when upstream changes cause
  overlapping offset pages, while still failing closed on repeated mismatch.
- Workday retries transient HTML outage responses instead of treating them as
  successful JSON payloads.
- The shared fetcher distinguishes exhausted retryable failures and passes
  timeouts to `httpcloak` in the units it expects.
- Reliability and parsing fixes for BambooHR, iCIMS, Oracle, The Hub, Uber,
  Workable, and several newly added providers.

## [0.2.0] — 2026-07-23

### Added — company discovery without ATS knowledge

Nobody knows OpenAI runs on Ashby. Two new package-root entry points
remove the need to know the `(ats, slug)` pair:

- `get_scraper_for_url("https://jobs.ashbyhq.com/openai")` — builds
  the right scraper from a public careers URL. Recognizes 20+ ATS URL
  shapes (path-tenant, subdomain-tenant, and full-URL platforms like
  Workday/Taleo/iCIMS). `resolve_careers_url(url)` exposes the raw
  `(ats, slug)` mapping.
- `find_company("openai")` — case-insensitive name/slug lookup over
  the hosted companies directory (`Client.companies()`, cached
  in-process; exact matches rank first).

### Added — `ScraperRegistry.has_scraper(ats)`

Skip dataset sources this package can't scrape yet without catching
`ScraperError` (GH-185). The hosted dataset can list a source before a
matching scraper ships — `search()` already tolerates that; this makes
the scraper side symmetric.

### Fixed

- Search filters now treat user input as literal text instead of a regular
  expression, so values containing characters such as `+`, `(`, or `[` work
  correctly and cannot trigger regex errors (GH-182).
- Unknown hosted dataset sources remain usable even when the installed
  package does not yet define a matching enum member or scraper (GH-185).

### Security

- Multi-tenant scraper constructors now validate slugs before interpolating
  them into hostnames or URL paths, preventing malformed tenant input from
  escaping the intended ATS origin.

## [0.1.0] — 2026-07-22

Initial release of `ats-scrapers`:

- A Python client for querying the hosted job dataset (`search`,
  `Client`, `list_ats`, `Manifest`), including a compatibility backfill for
  `global_id` when reading legacy schema-v2 dataset artifacts.
- A shared `Job`/`Company` schema and 52 scraper adapters for ATS
  platforms and job sources.
- Async-first scrapers: every adapter implements `async def afetch()`;
  the sync `fetch()` wrapper is safe to call from inside a running
  event loop (Jupyter, FastAPI) — it runs the coroutine on a worker
  thread instead of crashing in `asyncio.run`.
- A shared HTTP layer, `ats_scrapers.fetch.Fetcher` (exported at the
  package root): retries/backoff with `Retry-After`, status→exception
  mapping, client lifecycle, default headers, proxy configuration
  (`ATS_SCRAPERS_PROXY`, legacy 4-colon `PROXY`), and two engines —
  plain httpx and httpcloak TLS impersonation — with per-scraper
  declared escalation for 403/406-blocking load balancers.
- `WorkdayScraper.from_url(...)` documents the full-careers-URL slug
  contract; `include_descriptions` and `proxy` are `BaseScraper`
  constructor parameters; `Job.fetched_at` is timezone-aware UTC.
- Optional extras: `[parquet]` (full-snapshot search), `[scrapers]`
  (BYO scraping), `[all]` (both).

The installable package is the library only — dataset publishing and
orchestration live in the repo-only `pipeline/` directory. The package
is installed as `ats-scrapers` and imported as `ats_scrapers`. It
replaces the retired `jobhive-py` distribution.
