<p align="center">
  <img src="https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/assets/banner.jpeg" alt="ats-scrapers" />
</p>

# ats-scrapers

An open dataset and Python toolkit for job data from ATS platforms and public
sources.

[![PyPI](https://img.shields.io/pypi/v/ats-scrapers.svg?color=brightgreen)](https://pypi.org/project/ats-scrapers/)
[![Python](https://img.shields.io/pypi/pyversions/ats-scrapers.svg?color=brightgreen)](https://pypi.org/project/ats-scrapers/)
[![License](https://img.shields.io/badge/license-MIT-brightgreen.svg)](https://github.com/kalil0321/ats-scrapers/blob/main/LICENSE)

`ats-scrapers` provides two layers:

- A free, hosted dataset with **4.2M+ live jobs** from **63,000+ companies**
  across **49 sources**.
- More than 50 reusable scraper adapters, including Workday, Greenhouse, Lever,
  Ashby, SmartRecruiters, and SuccessFactors.

Jobs are collected from ATS endpoints, company career sites, and public job
feeds, then normalized into one typed schema. Querying the hosted dataset
requires no API key or account.

## Install

```bash
pip install ats-scrapers
```

The package is imported as `ats_scrapers`. Optional extras add only what you
need:

```bash
pip install "ats-scrapers[parquet]"   # query the full Parquet snapshot
pip install "ats-scrapers[scrapers]"  # run the scraper library
pip install "ats-scrapers[all]"       # install every runtime extra
```

## Query the public dataset

```python
from ats_scrapers import search

# Per-source searches work with the base install.
jobs = search(
    query="machine learning engineer",
    location="Paris",
    ats="greenhouse",
    limit=100,
)

# The result is a pandas DataFrame.
print(jobs[["company", "title", "location", "apply_url"]])
```

For practical full-dataset queries, install the `parquet` extra. The base
install is intended for smaller per-source CSV slices.

```python
from ats_scrapers import search

jobs = search(query="data engineer", remote=True, salary_min=80_000)
```

The [live manifest](https://storage.stapply.ai/jobhive/v1/manifest.json)
contains current row counts and artifact URLs. See the
[job schema](https://github.com/kalil0321/ats-scrapers/blob/main/JOB_SCHEMA.md)
for field definitions and normalization rules.

## Scrape a company

```python
from ats_scrapers.scrapers import get_scraper

scraper = get_scraper("ashby", "openai")
jobs = scraper.fetch()
```

Scraper classes are also available directly:

```python
from ats_scrapers.scrapers import GreenhouseScraper

jobs = GreenhouseScraper("anthropic").fetch()
```

Scraper adapters include:

- Major ATS platforms: Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  SuccessFactors, Oracle, iCIMS, Workable, Personio, and more.
- First-party company APIs: Amazon, Apple, Google, TikTok, and Uber.
- Public and regional sources: EURES, Bundesagentur, Arbetsformedlingen,
  Welcome to the Jungle, and others.

Run `ats-scrapers list-ats` for the sources currently present in the hosted
dataset.

## CLI

```bash
ats-scrapers search "platform engineer" --location Paris --ats greenhouse --limit 20
ats-scrapers scrape ashby openai
ats-scrapers list-ats
```

## Contributing

Contributions can add a source, improve an existing scraper, or add companies
to the CSV inventories in
[`ats-companies/`](https://github.com/kalil0321/ats-scrapers/tree/main/ats-companies).

```bash
git clone https://github.com/kalil0321/ats-scrapers
cd ats-scrapers
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## License

[MIT](https://github.com/kalil0321/ats-scrapers/blob/main/LICENSE)
