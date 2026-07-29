<p align="center">
  <img src="https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/assets/banner.jpeg" alt="ats-scrapers" />
</p>


# ats-scrapers

An open dataset and Python toolkit for job data from ATS platforms and public
sources.

[![PyPI version](https://img.shields.io/pypi/v/ats-scrapers)](https://pypi.org/project/ats-scrapers/)
[![Python versions](https://img.shields.io/pypi/pyversions/ats-scrapers)](https://pypi.org/project/ats-scrapers/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/kalil0321/ats-scrapers/blob/main/LICENSE)

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
[job schema](https://github.com/kalil0321/ats-scrapers/blob/main/docs/JOB_SCHEMA.md)
for field definitions and normalization rules.

## Scrape a company

You don't need to know which ATS a company uses. Paste its careers URL:

```python
from ats_scrapers import get_scraper_for_url

scraper = get_scraper_for_url("https://jobs.ashbyhq.com/openai")
jobs = scraper.fetch()
```

Or look it up by name in the hosted companies directory (63,000+ tenants):

```python
from ats_scrapers import find_company
from ats_scrapers.scrapers import get_scraper

find_company("openai")          # → ats="ashby", slug="openai", url=...
scraper = get_scraper("ashby", "openai")
jobs = scraper.fetch()
```

Scraper classes are also available directly:

```python
from ats_scrapers.scrapers import GreenhouseScraper

jobs = GreenhouseScraper("anthropic").fetch()
```

Scrapers are async-first — in async code (or for concurrency) use
`await scraper.afetch()` instead. The sync `fetch()` also works from
inside a running event loop (Jupyter, FastAPI): it transparently runs
on a worker thread.

Scraper adapters include:

- Major ATS platforms: ADP Workforce Now, Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  SuccessFactors, Oracle, iCIMS, HERP Hire, HRMOS, Paycom, Softgarden, Workable, Personio,
  and more.
- First-party company APIs: Amazon, Apple, Google, TikTok, and Uber.
- Public and regional sources: EURES, Bundesagentur, Arbetsformedlingen,
  Welcome to the Jungle, and others.

Run `python -c "from ats_scrapers import list_ats; print(*list_ats())"` for the
sources currently present in the hosted dataset. The dataset can list a source
before this package ships a scraper for it — `search()` still returns those
rows; only building your own scraper needs one. To skip sources without a
scraper:

```python
from ats_scrapers import list_ats
from ats_scrapers.scrapers import ScraperRegistry, get_scraper

scrapeable = [a for a in list_ats() if ScraperRegistry.has_scraper(a)]
```

## Contributing

Contributions can add a source, improve an existing scraper, or add companies
to the CSV inventories in
[`ats-companies/`](https://github.com/kalil0321/ats-scrapers/tree/main/ats-companies).
The scraper API is intentionally tiny: subclass `BaseScraper`, set `ats`, and
implement `async def afetch()` using `self.make_fetcher()` for HTTP — retries,
backoff, and error mapping come for free. See
`src/ats_scrapers/scrapers/greenhouse.py` for a compact reference and the `Job`
model in `src/ats_scrapers/models.py` for the schema you populate.

```bash
git clone https://github.com/kalil0321/ats-scrapers
cd ats-scrapers
uv sync --extra dev
uv pip install -r pipeline/requirements.txt  # repo-only ops/publisher tests
uv run pytest
uv run ruff check .
```

## License

[MIT](https://github.com/kalil0321/ats-scrapers/blob/main/LICENSE)
