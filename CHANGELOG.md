# Changelog

All notable changes to **ats-scrapers** are documented here. The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Command-line tools: `ats-scraper-search`, `ats-scraper-find`,
  `ats-scraper-fetch` and `ats-scraper-fetch-url`. Each prints JSON.
- onlyfy career pages (`onlyfy`).
- Pandapé career pages (`pandape`).
- CareerPlug hiring sites (`careerplug`).
- HiringThing job boards, on its own and on partner domains (`hiringthing`).
- isolved Hire career portals (`isolved_hire`).
- `Job.uuid`: the UUIDv7 the hosted dataset gives a posting the first time it
  publishes it, kept while the posting keeps its `global_id`.

### Changed

- `global_id` carries the board for the providers whose job ids repeat
  across employers (Avature, BambooHR, Beisen, Cornerstone, Eightfold, iCIMS,
  Jobvite, Personio, Phenom, SuccessFactors, Taleo):
  `icims:careers-acme.icims.com:6983` instead of `icims:6983`. In the
  2026-11-18 snapshot, 210,355 rows shared their `global_id` with a posting of
  another employer; 77 remain, each the same requisition listed twice by one
  employer. `Client.load()` builds the same ids for files without the column.
  Ids of the other providers are unchanged.
- Teamtailor: a career site served on the employer's own domain is read by
  passing its address as the slug.

### Fixed

- iCIMS: a board the catalog names only by its host label (`retail-shaneco`)
  takes the employer name from the posting page.
- TalentBrew: job links are read on sites whose paths are not in English.
- CareerPlug, jobs.ch, Y Combinator: when the page of one job fails, the reads
  still under way for the others stop with it.
- SuccessFactors: a site on the employer's own domain whose certificate has
  lapsed is read from SAP's name for it (`*.jobs2web.com`), whose certificate
  is checked. A site that refuses plain httpx (403/406) is read through
  httpcloak.
- Dayforce: a board whose services API answers only the employer's own
  addresses is read from the search of its career site.
- Eightfold: a board that refuses both job listings and has no careers page
  any more is reported as not found.
- `Fetcher`: escalating to httpcloak leaves the `User-Agent` to httpcloak,
  keeps `follow_redirects=False`, and returns each header as one string.

## [0.4.0] — 2026-11-19

### Added

- Structured fields on `Job`, set only when the source states them:
  `locations` (each advertised place of work, with city, region, country and
  whether it is the primary one), `workplace_type`, `compensation_components`
  (base salary, bonus, commission, equity or allowance, with unit, bounds,
  period and tier), `workload_percentages`, `job_category`, `engagement_type`,
  `education_requirements` and `skill_requirements` (with the priority the
  source gives them), and `source_created_at` / `source_updated_at`.
  `Client.load()` returns these columns, as nulls when a file does not carry
  them.
- Greeting career sites.
- Feishu Hire career sites.
- Zoho Recruit career sites.
- MyCareersFuture, the public job portal of Singapore.
- Manatal career pages.
- Jobberman (Nigeria, Ghana), BrighterMonday (Kenya, Uganda) and Emploitic
  (Algeria) job boards.
- Varbi employer RSS scraper with complete descriptions, public detail metadata,
  locale-independent tenant IDs, URL resolution, and a validated employer catalog.
  Malformed feeds fail closed. With detail enrichment enabled, removed or expired
  detail pages are excluded and mismatched pages or unrecognized deadlines fail
  closed. Date-only application deadlines are preserved in `Job.raw`.
- JD.com careers scraper: the openings of the whole group from the public
  list of `zhaopin.jd.com`, with their text, business unit, place and
  requisition number. The site has no page per opening, so each job links to
  the list page.
- Workable XML feed (`WorkableFeedScraper`): every job published on Jobs by
  Workable from the one file Workable offers for bulk use, one job per
  shortcode with all its locations and its URL unchanged. Workable shares the
  feed address on request (`feed_url` or `ATS_SCRAPERS_WORKABLE_FEED_URL`) and
  asks for at most one download per hour.
- `transport`: Workday and the scrapers that read through the shared `Fetcher`
  take an `httpx.AsyncBaseTransport` that sends their requests instead of a
  direct connection or a proxy, for example one that forwards them to another
  service. Pass it to the constructor, or set the `transport` attribute where
  the constructor does not list it. It is never closed by the scraper. Workday's
  `get_description` needs it to be an `httpx.BaseTransport` as well.
- `HostPacer`: starts requests to each host at most a given number of times per
  second, shared by every scraper it is given to, across threads and event
  loops. `key` paces groups of hosts together. Workday takes it as `pacer`.
- Bundesagentur: `proxies` takes routes in order of preference, `None` for a
  direct connection. A route that meets a 403, a 429 or a network error rests
  for five minutes while the next one carries the requests. A single `proxy`
  is used as well.
- `CloudBrowser`: a browser passed to a scraper as `cloud_browser` for pages
  plain HTTP cannot load. Avature reads boards that refuse httpx and httpcloak
  through it. `ats_scrapers.cloud_browser` rents one from Browserbase, Browser
  Use, Kernel or Notte with your own API key (`cloud-browser` extra), keeps one
  session for every page and stops it on `close()`, `aclose()` or the end of a
  `with` block. Other browsers fit by subclassing `CloudBrowser`.

### Changed

- Descriptions keep the text the employer supplied. Job Bank Canada, Workable,
  Y Combinator, Meta and jobs.ch read the full posting when the listing has no
  body, Wanted and MANFRED add the sections they left out, and Workable drops
  the widget header and Apply footer.
- EURES no longer builds a description from skills, application instructions
  or employer text, and jobs.ch and Programathor no longer use the page's SEO
  summary: a posting without a body has no description.
- The hosted dataset moves to `https://storage.stapply.ai/jobs/v1/`, the new
  default manifest URL. `jobhive/v1` keeps working as a mirror.
- Dataset downloads (`search`, `Client`, `Manifest.fetch`) identify themselves
  as `ats-scrapers/<version>` in the `User-Agent` header. A client you pass in
  keeps its own headers.
- Workday: `get_description` keeps its connection to the site open between
  jobs. `close()` releases it.
- Paylocity reads the job pages of a board eight at a time instead of two.
- Avature uses Browserbase only through `cloud_browser=BrowserbaseBrowser()`. It
  no longer reads `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` by itself,
  and `ATS_SCRAPERS_DISABLE_BROWSERBASE` is gone: without a `cloud_browser`, a
  refused board raises.

### Removed

- The dataset pipeline (`scripts/`, `pipeline/`) and the snapshot archive
  workflow. The library and the company lists are unchanged.

### Fixed

- Avature: a board behind an AWS WAF JavaScript challenge is read in the
  `cloud_browser` when one is given, instead of raising. Without one it raises
  as before.
- Cloud browsers read a page behind an AWS WAF challenge once the challenge has
  reloaded the real page, and read again a page that is still navigating.
- jobs.ch: a 403 that follows answered requests pauses every search request
  (30 seconds, growing to 300) and resumes at a slower pace, instead of skipping
  the refused date windows and pages. A block that outlasts every pause fails
  the fetch instead of returning part of the board.
- jobs.ch: a posting's detail that its route refuses (403) or cannot reach is
  read through the other route, the scraper's own address or `PROXY`, and the
  next detail starts on the route that last answered.
- Paycom: a listing that changes while it is read, a job posted or closed
  between two pages, is read again from the start, up to 3 reads, instead of
  failing the count check.
- WinTalent: a recruit type whose pages disagree, page 1 being served from a
  stale cache, is read again from page 1 after 330 seconds, up to 3 reads,
  instead of failing.
- Programathor: badge text nested in a job card's title is removed from the
  title.
- Fetcher: the timeout given to httpcloak is passed in seconds, the unit
  httpcloak expects, instead of being multiplied by 1000.
- Avature: a board refused with 406 to both httpx and httpcloak raises instead
  of returning no jobs. The `proxy` argument is now used for listing and job
  pages.
- Avature: job links to `FolderDetail` pages (Epic, KPMG Ireland, Rosewood,
  Smurfit Westrock) and to Maximus's `Job-Description` pages are read, and
  `folderOffset` pages are followed. Share links (`mailto:`) are no longer read
  as a job.
- Avature: a first listing page without a job link raises when it states a
  result count above zero, and a board that redirects to a site that is not
  Avature is reported as not found.
- Tesla: the browser goes through the `proxy` argument when one is given, and
  through the `PROXY` environment variable only otherwise.
- Eightfold: a listing page that answers 405, which Eightfold returns on every
  tenant for a few minutes once an address has sent too many requests, is retried
  after 30, 90 and 180 seconds instead of failing the board at once. A board
  still refused after that raises a `blocked (405)` error.
- Workday: listing and description requests go through `httpcloak` when it is
  installed. Workday's bot protection answers many plain Python clients with an
  HTTP 429 challenge from the first request, whatever the request rate. Without
  `httpcloak`, requests go through httpx as before and a warning says so.
- Workday: when one request of a board fails, the board's other searches and
  description requests are cancelled before its connection closes. They no
  longer run on after the error and fail with "the client has been closed".
- Workday: a bot challenge (HTTP 429 or 403 marked `cf-mitigated: challenge`)
  ends the board at once instead of being retried, since a retry gets the same
  challenge. Other 403 and 429 answers wait the `Retry-After` the server gives
  (seconds or a date) and end the board when it is longer than 30 seconds;
  without one they wait about 5 then 10 seconds, with jitter. A retry no longer
  holds one of the board's request slots while it waits, no wait follows the
  last attempt, and no wait runs past `max_fetch_seconds`. The error still names
  the last HTTP status. Descriptions stop being requested after one is refused
  with 429 or a challenge, and a warning says so.
- Bundesagentur: a request refused with 403 or 429, a server error or a network
  error is retried with backoff capped at two minutes, up to 14 attempts (about
  16 minutes), while other pages go on, so one refused page no longer ends a full
  scrape. The scrape still fails when a page stays refused that long or a run
  meets 5,000 such failures. `transient_failures` counts them by type.
- Bundesagentur: a slice that shrinks while it is read stops at its new end. A
  page past the end of the results counts as the last page instead of an invalid
  response that failed the whole scrape.
- Phenom: a board with more than 9,900 jobs is split by category and state, so every
  job is collected instead of the first 9,900.
- Oracle: a site with more than 10,000 jobs is read in two parts by posting date, so
  every job is collected instead of the first 10,000.
- Avature: listing pages follow the portal's own pagination links, so a portal that
  serves fewer jobs per page than asked, pages by `projectOffset`, or redirects to
  another host is read to the end instead of stopping after the first page.
- Avature: job links of the form `JobDetail?jobId=N` are read, so portals such as
  Booz Allen no longer return no jobs.
- iCIMS: a board is read past 200 pages (up to 2,000), and a board past that raises
  instead of returning the first 4,000 jobs.
- Zoho Recruit: a career site that lists 750 jobs, the most one page carries, is
  logged as a warning, since the site may have more.
- Beisen legacy: a tenant that moved to the current Beisen portal raises instead of
  returning no jobs, and a closed tenant is reported as not found. Anmei Group is
  listed under `beisen`.
- Moka: a board is read up to 50,000 jobs instead of 10,000, and a board past that
  raises instead of returning a cut list.
- Workday: a board above the 2,000-job search cap is also split by the country and
  city facets Workday nests under `locationMainGroup`, and no longer by the
  `distance` facet, which never splits it. A facet that leaves jobs without a value
  is used only when no other one covers them all, and a search that still cannot be
  split below the cap is logged.
- TalentBrew: a site above 10,000 postings is read newest first and then oldest
  first, so up to 20,000 postings are collected instead of the first 10,000.
- Amazon: the search total stops at 10,000, so the board is read per business category
  and every job is collected.
- Arbetsförmedlingen: one search returns at most 2,100 ads, so the board is split by
  publication date until each part fits, and a refused page raises instead of ending
  the walk.
- jobs.ch: one search returns at most 2,000 postings, so the board is split by
  publication date until each part fits. `query_seeds` now defaults to none.
- Y Combinator: the hiring filter stops at 1,000 companies, so the full company list
  is walked and the hiring companies kept.
- The Hub: every market is listed; the unfiltered list holds only remote jobs.
- jobs.cz: asks for the classic listing, since the beta site some visitors are sent to
  has no listings in its HTML and returned no jobs.
- Built In: waits out Cloudflare's short rate-limit challenge instead of ending the
  walk, and reads up to the listing's last page (1,000) instead of 200.
- EURES: slices above the 10,000-result cap no longer lose vacancies. Queries are
  split by every filter the portal accepts (location, contract type, experience,
  education, EURES flag, sector, schedule, languages, benefits, then ISCO
  occupations), each split is checked against its parent total, and vacancies left
  out are logged as warnings. Vacancies skipped because the portal reorders results
  with the same timestamp between pages are read again.
- EURES: reads the schedule list of each vacancy, so a vacancy without a contract type
  still gets a full-time or part-time employment type.
- Recruitee: a board whose careers site is closed, whose public API is disabled, or
  whose custom domain now sends readers to another job site is reported as not found.
- Wanted: requests are no longer rejected with 403 by the site's firewall, and
  description requests are paced under its rate limit.
- Wellfound: reads the job data embedded in every page of each role, descriptions
  included, without Firecrawl. Listings imported from an ATS are skipped unless
  `include_ats_imports=True`. `firecrawl_api_key` stays available; the
  `FIRECRAWL_API_KEY` variable alone no longer enables it.
- Bundesagentur: a search slice above the API's 10,000-result cap with no
  exact facet split is collected by German state and country, and the
  official sort orders fill in postings located only as "Deutschland". Full
  scrapes no longer stop on such a slice.
- Job Bank Canada: full scrapes read every result page the announced count
  implies, then read them again while postings are missing. The site's
  search replicas order postings of the same date differently from one
  request to the next, so a single walk repeated some postings and skipped
  others, and stopped partway.
- Meta, Tesla: `cloakbrowser` is pinned to 0.6.5, the last release that
  launches without a license key, with `playwright<1.65`.
- Meta: reads the renamed `job_search_with_featured_jobs_v2` field.
- Pipeline: a few failed boards no longer hold back ADP, Jobvite, Keka, PageUp, Paycom or Varbi.
- Lever, Gem, Recruitee: a board whose public page is closed is reported as not found.
- Avature: a tenant whose host no longer resolves is reported as not found, and
  a tenant behind an AWS WAF JavaScript challenge says so in its error. The
  Avature company row points at its own portal (`careers.avature.net/en_US/main`).
- PageUp: a tenant that moved to PageUp's newer career site is read from its
  RSS feed, with complete descriptions, instead of failing.
- Jobvite: a board that redirects away from Jobvite is reported as not found, a
  board that shows "No results found." is empty, job pages that the employer
  redirects to its own site are read on Jobvite, and a job that the unstable
  list order hides at a page boundary is fetched again. The catalogue drops
  Jobvite's demo tenant `piedpiper`.
- Keka: a tenant that no longer exists or has switched its careers portal off
  is reported as not found.
- HRMOS: a board that says it has no published jobs is empty instead of failing.
- Phenom: job links open the posting.
- Oracle: job links open the posting.
- Amazon: job links open the posting.
- Beisen: job links open the posting.
- Cornerstone: job links open the posting.
- Moka: job links open the posting. The scraper built
  `{tenant}/job/{id}`, which answers 404 on Moka's career sites; the posting
  lives at `{tenant}#/job/{id}`. Every Moka job published so far carried the
  dead form (32,610 rows in the 12 Nov 2026 snapshot).
- Ashby: jobs of employers whose hosted pages are closed no longer carry a
  link that shows "not found". 23 employers (Saronic, Form Energy, Cursor,
  Lemonade, Supercell, PostHog) list their openings on their own site; the
  API's `jobUrl` answered 404 for their 897 jobs on 14 Nov 2026. The scraper
  checks the hosted page with one HEAD request and, when it is closed, links
  each job to the embed view of its posting, which opens that posting, and
  records the employer's careers page in `raw["careers_page"]`. With the API
  off the scraper now reads the embed view, which also answers when the
  hosted pages are closed.
- Ashby: when the public API answers 404, the scraper reads the hosted board
  page (`jobs.ashbyhq.com/{slug}`) and each posting's page instead of reporting
  the employer as gone. Eleven employers with 298 openings on 14 Nov 2026 have
  the API off and the page public (Whatnot, EvenUp, Lime, Chainlink Labs). A
  board that answers 404 on both is still reported as not found. The catalogue
  gains those eleven and four more boards the API serves (monday.com, plancraft,
  Marathon Fusion, source.dev): 439 openings in all.
- Cross-source dedup, title passes (`pipeline/crossdedup.py`). A kept row now
  absorbs at most one row per other source and per place it lists, and the
  better source keeps all of its rows: one title advertised for 200 shops stays
  200 postings instead of one. Rows without a country are compared too (their
  place is judged on the city), employer names in non-Latin scripts get a key,
  and titles are compared without case, accents, punctuation, gender tags or a
  closing bracket. The fuzzy pass no longer treats a title contained in a
  longer one as the same job.
- `country_iso` derived from the location: a trailing `, CA` is California
  unless a Canadian province or city precedes it, and `Wilmington, DE` is
  Delaware. Both were published as Canada and Germany.
- Eightfold detail enrichment retries 429 responses using `Retry-After` or
  exponential backoff, and retries 5xx responses with linear delays. It stops
  the per-job detail calls for the rest of the run after a 403 or 405 block,
  or after a 429 that asks to wait more than a minute.
- Paycom: a retired portal ("Job board does not exist") is reported as not found.
- SuccessFactors: a site is reported as not found when it is deactivated, when
  its feed redirects away from a feed (to the employer's homepage or another
  ATS), when its host now serves Workday, does not resolve or presents a
  certificate for another name, and when a legacy career site does not know
  the company.
- SuccessFactors: legacy XML feeds of large employers, which take close to a
  minute to start, no longer time out.
- Catalogue: AkzoNobel, Bechtel and Viega move from SuccessFactors to Phenom,
  Gateway Foundation's site is now Alpharus Health's, MANN+HUMMEL's row points
  at `careers.mann-hummel.com`, and a duplicate Suntory Global Spirits row is
  removed.
- Cornerstone: a career site whose `{slug}.csod.com` host no longer resolves is
  reported as not found.
- Softgarden: a site whose domain belongs to no client, or to a removed
  company, is reported as not found.
- Phenom: requests name the client (`Mozilla/5.0 (compatible; ats-scrapers/1.0)`)
  instead of a bare `Mozilla/5.0`, which Akamai held until timeout on
  Electrolux's site. A site whose search page redirects to a page that is not
  Phenom's (KBR, SEI, Morningstar), or whose host no longer resolves, is
  reported as not found. The catalogue gains Trinzic.
- Workday: a tenant that Workday reports as moved to another data center
  (410 `ERR_TENANT_MIGRATED`), or that its host no longer serves (422
  `HTTP_422` on the first search), is reported as not found. A search that
  gives up after its retries names the last status or network error instead
  of `None`. Catalogue: Advocate Health, Alight, CliftonLarsonAllen, Carnegie
  Mellon (two sites), Comcast, Kohl's, Lilly (two sites), Lithia & Driveway,
  OneDigital, Petco, Saab and USIC point at their current data center, and a
  duplicate Stanford Health Care row is removed.
- Personio: a board that answers 403, 429 or anything but a JSON job list on
  one of its paths raises an error instead of being reported as not found or
  empty, a careers site that answers 404 on every path is reported as not
  found instead of empty, and a board with no openings is empty.
- join.com: a company that join.com deleted (410 "Resource deleted") is
  reported as not found.
- Fetcher: `missing_host_not_found` also covers a host whose CDN can no
  longer resolve the origin (Akamai's 503 "DNS failure" page on the last
  attempt).
- Oracle: a retired pod, whose host no longer resolves or whose CDN answers
  "DNS failure", is reported as not found.
- iCIMS: a portal that asks for a password, closed to the public, is reported
  as not found.
- Darwinbox: a tenant that Darwinbox does not host ("Invalid subdomain") is
  reported as not found.
- Beisen: a portal switched off with the notice 企业已暂停招聘 ("recruiting
  paused") is reported as not found; another notice is quoted in the error.
- Eightfold: a host whose company group no longer exists ("Group ID not
  found") is reported as not found.
- Paycom: a page that comes back one row short, followed by a page that
  repeats a row, no longer fails the count check; jobs are counted once.
- Catalogue: Sapiens leaves SuccessFactors; `careers.sapiens.com` now
  redirects to HiBob.

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
