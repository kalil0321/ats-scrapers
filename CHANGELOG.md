# Changelog

All notable changes to **ats-scrapers** are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — async-first scrapers and a shared fetch layer

- Every scraper now implements `async def afetch()`; the sync
  `fetch()` wrapper remains and is now safe to call from inside a
  running event loop (Jupyter, FastAPI) — it runs the coroutine on a
  worker thread instead of crashing in `asyncio.run`.
- New `ats_scrapers.fetch.Fetcher` (exported at the package root):
  one shared implementation of retries/backoff with `Retry-After`,
  status→exception mapping, client lifecycle, default headers, proxy
  configuration (`ATS_SCRAPERS_PROXY`, legacy 4-colon `PROXY`), and
  two engines — plain httpx and httpcloak TLS impersonation — with
  per-scraper declared escalation for 403/406-blocking load
  balancers. Scrapers no longer hand-roll any of this.
- `include_descriptions` and `proxy` are `BaseScraper` constructor
  parameters (assigning the attribute post-construction still works).
- `WorkdayScraper.from_url(...)` makes the full-careers-URL contract
  explicit.
- `Job.fetched_at` is now timezone-aware UTC.

### Removed — narrower package scope

The installable package now contains only the library: the dataset
client and the scrapers.

- **CLI removed** (`ats-scrapers search/scrape/publish/list-ats` and
  the console entry point). This is a library first; `list_ats()` is
  exported from the package root as the replacement for `list-ats`.
- **`ats_scrapers.storage` removed from the package.** The R2 client
  and dataset publisher are ops code for maintaining the hosted
  dataset; they now live in the repo-only `pipeline/` directory with
  their own `pipeline/requirements.txt`. The `publish` extra is gone.
- **`ats_scrapers.discovery` removed** — it was an empty stub. The
  `discovery` extra (firecrawl-py) is gone with it.
- The `all` extra is now `[scrapers,parquet]`.

## [0.1.0] — 2026-07-22

Initial release of `ats-scrapers`:

- A Python client for querying the hosted job dataset.
- A shared schema and more than 50 scraper adapters for ATS platforms and job sources.
- A CLI for searching the dataset, running individual scrapers, and listing sources.
- Optional extras for Parquet, scraping, discovery, and publishing workflows.

The package is installed as `ats-scrapers` and imported as `ats_scrapers`. It
replaces the retired `jobhive-py` distribution.
