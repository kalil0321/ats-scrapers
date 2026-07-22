# Changelog

All notable changes to **ats-scrapers** are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
