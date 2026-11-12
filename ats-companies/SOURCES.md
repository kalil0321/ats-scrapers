# Seed sources for tenant discovery

This file lists the outside sources used to find candidate tenants. It
covers the rows added from those sources, not every row in these CSVs.

A candidate from one of these sources is added only after the provider's
live job list (its API or its public listing page, as
`docs/DISCOVERY_RULES.md` allows) returns at least one job. The sources
are candidate lists only.

| Dataset | Licence | Used for |
| --- | --- | --- |
| [latmay/ats-career-page-urls](https://huggingface.co/datasets/latmay/ats-career-page-urls) (Hugging Face, Nov 2026 snapshot) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Candidate career-page URLs for Workday and other providers |
| [Common Crawl](https://commoncrawl.org/) URL index (CC-MAIN-2026-04 to CC-MAIN-2026-39) | [Common Crawl terms of use](https://commoncrawl.org/terms-of-use) | Candidate board URLs for Workday, Greenhouse, Ashby, BambooHR, Dayforce, iCIMS, Paylocity, Personio, Recruitee, Softgarden and Teamtailor |

Some Workday career-site URLs were also found through web search (Exa) for
`myworkdayjobs.com` pages, then validated the same way.

Common Crawl was read only through its public CDX index (URLs and HTTP
status), never by downloading page content.

Data under a non-commercial licence (for example CC BY-NC) is not used.
