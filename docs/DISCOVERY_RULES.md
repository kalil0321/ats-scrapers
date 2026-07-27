# Discovery Rules

This document is the operating guide for adding provider/company rows to
`ats-companies/*.csv`. The goal is not to collect every URL that looks related
to an ATS. The goal is to add rows that the repository's scrapers can fetch
directly, repeatedly, and without landing on generic search pages, expired job
details, or ATS home pages.

## Baseline Rule

A candidate is valid only when all of these are true:

1. The row points at the same provider surface that the scraper consumes.
2. The URL or slug is company-specific, not a provider home page or global
   marketplace/search page.
3. The validation request returns HTTP 200 or the provider-specific success
   status.
4. The response contains live job-list data, not just a branded shell.
5. The candidate is not already present on `main` or in an open provider PR.
6. The company name is readable and tied to the actual tenant.

Do not add rows only because a search result, third-party job board, or expired
job detail references the ATS.

## Required Workflow

1. Start from current `main` and check open draft and non-draft PRs for the same
   provider.
2. Read the scraper and runner mapping before discovery. CSV columns must match
   what `scripts/run_pipeline.py` passes into the scraper.
3. Discover candidates with broad search, but validate with the provider's real
   listing API or listing page.
4. Deduplicate by the provider's stable tenant identity, not just by URL string.
5. Keep rejected candidates in notes while working so future automation can
   learn from invalid cases.
6. Run focused provider tests and the full test suite before opening a PR.
7. Open one draft PR per provider.

## Discovery Strategies

### Search Engine Queries

Use search engines or local SearXNG to find likely tenant URLs, then validate
them independently.

Useful patterns:

- `site:{provider-domain} "{provider-specific path}"`
- `"{provider-specific API path}" "{tenant parameter name}"`
- `"{provider-domain}/{known path}" "Careers"`
- `"{provider-domain}" "{job id pattern}"`
- `"{provider-domain}" "{provider branding text}"`

Examples:

- Taleo: `"tbe.taleo.net" "ats/careers/v2/searchResults"`
- Taleo: `"tbe.taleo.net" "searchResults?org=" "cws="`
- Gem: `"https://jobs.gem.com/" "Careers"`
- Gem: `"jobs.gem.com/" "am9icG9zd"`
- Eightfold: `site:eightfold.ai/careers "domain" "configPath"`
- Oracle: `"CandidateExperience" "recruitingCEJobRequisitions"`
- ADP Workforce Now: `"workforcenow.adp.com" "recruitment.html?cid="`
- SuccessFactors: `"sitemal.xml" "successfactors"`

Search results are discovery hints only. They are not validation.

### Firecrawl-Style Mapping

For providers with public hosted job-board paths, use a crawler/map step to
enumerate URLs under the provider host, then reduce to candidate board roots.

Rules for map output:

- Normalize to the board/listing root, not individual job pages.
- Discard pages outside the provider host unless the scraper supports custom
  domains.
- Discard URLs with tracking-only query variants.
- Validate each normalized candidate against the provider API or listing page.

This is useful for hosts like `jobs.gem.com`, `*.eightfold.ai`, and public ATS
subdomains where job detail pages reveal the board slug.

### Provider APIs and Sitemaps

Prefer structured validation over HTML scraping when available:

- GraphQL listing queries.
- JSON listing endpoints.
- XML sitemaps that enumerate live jobs.
- RSS/XML job feeds.
- Official public tenant APIs.

Do not use an endpoint that requires private credentials unless the credential
is already part of the repo workflow and documented.

## Invalid Cases

Reject these even if they appear in search results:

- Provider home page, such as the ATS marketing site.
- Global marketplace/search page that mixes many companies.
- Generic ATS login page.
- Generic branded shell with no job data.
- HTTP 404 page that still leaves stale jobs accessible through an API.
- Redirect from a company board URL to a provider home page or global search.
- Expired individual job page when the board/listing URL is not valid.
- Demo, sandbox, stage, QA, test, or recruiting-vendor sample tenants.
- Opaque technical slugs when no reliable company name can be derived.
- Duplicate tenant identity with only a different locale, tracking parameter,
  or alternate frontend path.

When in doubt, skip the candidate. A small accurate PR is better than a large
dirty one.

## Provider Notes

### Gem

Scraper input:

- CSV columns: `name,slug,url`
- `slug` is the Gem board ID.
- `url` should be `https://jobs.gem.com/{slug}`.
- The scraper calls `POST https://jobs.gem.com/api/public/graphql/batch` with
  operation `JobBoardList`.

Validation:

1. Fetch `https://jobs.gem.com/{slug}` with redirects enabled.
2. Require HTTP 200.
3. Require final URL to equal the submitted board URL after trimming one
   trailing slash.
4. Reject `404 page not found` and generic-only titles such as just `Careers`
   unless there is another reliable employer identifier.
5. Run the exact `JobBoardList` query used by the scraper.
6. Require non-empty `oatsExternalJobPostings.jobPostings`.

Known edge case:

- Some slugs still return jobs from GraphQL while the hosted board URL returns
  404. Do not add those to `gem.csv`; the row URL would be misleading and may
  break users that inspect company boards.

### Taleo

Scraper input:

- CSV columns: `name,slug,url`
- `slug` and `url` are both the full Taleo search-results URL.
- Valid URLs look like
  `https://{shard}.tbe.taleo.net/{tenant}/ats/careers/v2/searchResults?org={ORG}&cws={CWS}`.

Validation:

1. Normalize query strings to the required `org` and `cws` parameters.
2. Fetch the `searchResults` page.
3. Require HTTP 200.
4. Require at least one `viewJobLink` anchor or equivalent live job link.
5. Deduplicate by `org`, then choose one public board URL per org.
6. Prefer company names from job detail JSON-LD `hiringOrganization.name`.

Known edge cases:

- One org can have many `cws` values. Do not add every `cws`; choose the best
  representative board.
- Reject demo/sandbox orgs such as `RDASH_DEMO*`.
- Reject generic names like `Career Centre` when no other employer identity is
  available.

### Eightfold

Scraper input:

- CSV columns: `name,slug,url,domain`
- Default API host is `https://{slug}.eightfold.ai`.
- `domain` may be required and must match the tenant's configured domain.
- Some custom domains require `url` to supply `base_url`.

Validation:

1. Read the public careers page and extract the configured domain from
   `pcsx-data` or embedded config when possible.
2. Call `{base_url}/api/pcsx/search` with the scraper's parameters:
   `domain`, empty `query`, empty `location`, `start=0`, and
   `sort_by=timestamp`.
3. Require HTTP 200 and non-empty `data.positions`.
4. Reject `PCSX is not enabled for this user` for the current scraper unless
   the scraper is extended to support that tenant's alternate API.
5. Reject tenants that only work through `/api/apply/v2/jobs` until the scraper
   supports that SmartApply shape.

Known edge cases:

- A visible Eightfold careers page can be a login or branded shell while the
  PCSX API is disabled.
- Some valid-looking public pages use `app.eightfold.ai` with a domain query
  and are not company-specific enough for a normal `{slug}.eightfold.ai` row.
- WAF-blocked tenants may need `httpcloak`; distinguish WAF from wrong tenant
  or wrong API.

### Oracle CandidateExperience

Scraper input depends on the Oracle runner mapping, but rows should represent a
real CandidateExperience site and site number/path, not an Oracle marketing or
generic jobs page.

Validation:

1. Extract the candidate experience host and site number.
2. Call the public `recruitingCEJobRequisitions` endpoint with the same
   `finder=findReqs;siteNumber=...` parameters expected by the scraper.
3. Require HTTP 200.
4. Require non-empty requisition items.
5. Reject duplicate locale paths, expired single requisition links, and generic
   Oracle Cloud pages.

### ADP Workforce Now

Rows must use the public Workforce Now recruitment URL with both the tenant
``cid`` and career-center ``ccId`` query parameters.

Validation:

1. Call the public ``job-requisitions`` endpoint with the candidate ``cid``,
   ``ccId``, locale, ``$skip``, and ``$top`` values.
2. Require HTTP 200, a non-empty ``jobRequisitions`` list, and a numeric
   ``meta.totalNumber``.
3. Fetch at least one posting through its ``ExternalJobID`` detail endpoint.
4. Require the detail ``itemID`` to match the listing and require a non-empty
   real ``requisitionDescription``.
5. Reject login-only pages, internal career centers, expired tenant IDs, and
   duplicate URLs that differ only by ``jobId`` or tracking parameters.

### SuccessFactors

Validation:

1. Validate the exact host from the CSV.
2. Fetch `https://{host}/sitemal.xml`.
3. Require HTTP 200.
4. Parse XML/RSS successfully.
5. Require at least one `<item>` or live job entry.
6. Reject stage, QA, sandbox, and test hosts even when they return XML.

### Recruitee

Validation:

1. Candidate tenant should be the Recruitee company subdomain/slug used by the
   scraper.
2. Call the public offers endpoint used by the scraper, usually `/api/offers`.
3. Require HTTP 200 and non-empty offers.
4. Reject marketing pages, archived boards, and third-party pages that only
   mention the Recruitee board.

### Pinpoint

Validation:

1. Candidate tenant should map to a public Pinpoint company board.
2. Call the JSON postings endpoint used by the scraper, usually
   `/postings.json`.
3. Require HTTP 200 and non-empty postings.
4. Reject generic Pinpoint pages and inactive boards.

### Cornerstone

Validation must prove both URL validity and name quality:

1. Use a real tenant-specific careers endpoint, not a generic search or login.
2. Confirm the scraper can fetch jobs from the row.
3. Use the CSV `name` as the human-readable company name when the provider
   exposes only technical slugs.
4. Reject rows where the final company name would be an unreadable slug unless
   a reliable name source is found.

### Avature

Validation:

1. Preserve full URL slug casing when the tenant requires it.
2. Validate against the actual Avature listing endpoint or public jobs page
   consumed by the scraper.
3. Reject case-normalized URLs that redirect or fetch a different tenant.

## Automation Hints

An automated discovery agent should keep separate stages:

1. `discover`: collect raw URLs and source snippets.
2. `normalize`: reduce job/detail URLs to provider tenant identities.
3. `validate`: call provider-specific live listing APIs.
4. `classify_invalid`: record redirects, 404s, generic pages, empty jobs, demo
   hosts, API-disabled tenants, and duplicate identities.
5. `name`: derive human-readable company names from structured metadata,
   JSON-LD, page title, or trusted source context.
6. `dedupe`: compare against `main`, existing local changes, and open PRs.
7. `emit`: write CSV rows only for candidates that pass validation.

Store validation evidence for every emitted row: source URL, final URL, status,
job count, detected company name, tenant key, and invalid-reason if skipped.

## PR Checklist

- One provider per PR.
- Branch from current `main`.
- Include only CSV or scraper changes needed for that provider.
- State discovery source and validation method in the PR body.
- Mention known rejected cases if they explain why the PR is conservative.
- Run focused tests.
- Run `uv run pytest -q`.
- Watch GitHub CI until all checks pass.
