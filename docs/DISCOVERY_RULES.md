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

### Recruiterbox (Trakstar Hire)

Rows use the account name (``client_name``) as ``slug``. The pipeline reads
the ``slug`` column only. New rows use ``https://{slug}.hire.trakstar.com``
as ``url``. Older rows carry ``https://{slug}.recruiterbox.com``, the host
before the rename to Trakstar Hire; it redirects to
``https://{slug}.hire.trakstar.com`` and is left as it is. Validate every
row, old or new, on its ``hire.trakstar.com`` pages.

Validation:

1. A live count from the feed
   (``jsapi.recruiterbox.com/v1/openings?client_name={slug}``) is not enough.
   The feed goes on listing the openings of an account after the employer
   has left Trakstar Hire. Open one job page (``hosted_url``): a closed
   account answers 404 with "This employer is no longer using Trakstar Hire
   to collect applications". Reject it. Request the page only when its host
   is ``{account}.hire.trakstar.com`` (``hosted_url`` comes from the feed),
   and do not follow redirects. The scraper makes the same check, with the
   same limits, and reports such a tenant as not found.
2. Do not judge by the career home page (``https://{slug}.hire.trakstar.com/``):
   it answers 404 for some accounts whose job pages work.
3. An account can be reached under an earlier name. The feed then answers
   the same openings, with the current name in ``client_name`` and in the
   host of ``hosted_url``. List the current name only: under an earlier name
   the display name of the row is not applied, and a second row adds
   requests and no posting.
4. Take the employer name from the page: the career home page title
   (``X jobs | X openings | X careers``) or the job page title
   (``{job title} at X``). Do not use the account name as the employer name.
5. Read the openings before adding a row. Reject test accounts and accounts
   whose page names no employer (``No``, ``Editor``).

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

### Lever

Lever hosts its customers in two regions with separate APIs. A board on
``jobs.lever.co`` is read from ``api.lever.co``; a board on
``jobs.eu.lever.co`` is read from ``api.eu.lever.co`` and answers 404 on the
other one.

- ``url`` is the board URL, and its host gives the region.
- ``slug`` is what ``get_scraper("lever", slug)`` is given. For a US board it
  is the board slug, in the case the board URL uses. For an EU board it is
  the board URL (``https://jobs.eu.lever.co/{slug}``), because a bare slug
  is read from the US API.
- Validate against the API of the row's region, and require the board page
  itself to answer 200.
- List a board slug once, in one region. The two regions are separate
  namespaces, so the same slug can be another company in the other region,
  but the pipeline keys a row's display name by its bare board slug: two
  rows with one slug would share a name. If both boards are live, keep the
  one that was validated and leave the other out.
- The employer name is the board page title without its `` jobs`` suffix.

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

### Frontline AppliTrack

Rows use the board URL ``https://www.applitrack.com/{tenant}/onlineapp`` as
both ``slug`` and ``url``, the employer's name as ``name`` and its country
(``US`` or ``CA``) as ``country_iso``. Most boards belong to school
districts.

Validation:

1. Run the scraper. It reads the board's all-postings endpoint
   (``JobPostings/Output.asp?all=1``) and requires the number of postings
   it parsed to match the number the board announces.
2. Add a board only when it lists at least one posting.
3. Keep a listed board that has no posting today: school hiring is
   seasonal and the board fills again.
4. Remove a board whose all-postings endpoint and home page both answer an
   error status (HTTP 500, 503 or 404): the employer has left AppliTrack.
5. A consortium board repeats the postings of its member districts, with
   the same posting id and the district as employer. Both rows stay; the
   pipeline publishes a posting id once.

### Jibe (iCIMS career sites)

Rows use the career-site origin on the employer's own domain
(``https://careers.example.com``) as ``url`` and the host as ``slug``.

Validation:

1. Call ``https://{host}/api/jobs?page=1&limit=100&internal=false``.
2. Require HTTP 200, a JSON object with a ``jobs`` list and a numeric
   ``totalCount`` above zero.
3. Require each posting to carry ``data.slug`` and ``data.title``.
4. Reject hosts that answer the path with HTML, a 404 or a login page:
   they run another career-site product.

### SuccessFactors

Validation:

1. Identify whether the row is a Recruiting Marketing host or a legacy
   Recruiting Management URL with a `company` query parameter.
2. For Recruiting Marketing, fetch `https://{host}/sitemal.xml`, parse RSS,
   and require at least one `<item>`.
3. For legacy Recruiting Management, call `/career` with the exact `company`
   ID, `career_ns=job_listing_summary`, and `resultType=XML`.
4. Require HTTP 200, a `<Job-Listing>` root, and at least one `<Job>` with a
   non-empty `ReqId`, `JobTitle`, and real `Job-Description`.
5. Deduplicate legacy rows by the stable `company` ID and remove superseded
   empty Recruiting Marketing feeds for the same employer.
6. Reject login-only responses, generic career shells, stage, QA, sandbox,
   and test tenants even when they return HTTP 200.

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

### CATS

Rows use the tenant's host label as ``slug`` and
``https://{slug}.catsone.com/careers/`` as ``url``. Most customers are
staffing and recruiting firms; the row is the firm, which is the one that
posts the job.

Validation:

1. Fetch the portal. Require HTTP 200 and the portal's data in the page,
   with a portal that does not say it is inactive.
2. Run the scraper and require at least one job.
3. Require a job posted in the 90 days before validation: its
   ``datePosted``, which the list gives and, failing that, the job page.
   Many firms leave a portal online with the jobs it had when they stopped
   using it.
4. The row name is the portal's own name (``portal.name`` in the page's
   data). When that is a generic word or a lower-case label
   ("Recruitment", "rsg"), take the firm's name from the site the portal
   links to (``portal.siteUrl``), as that site states it.
5. A tenant can have several portals (``/careers/{number}``). The row is
   the default one, where ``/careers/`` leads. Add another portal of a
   tenant, as ``{slug}/{number}``, only when it lists jobs the default one
   does not: a job has the same number on every portal and is published
   once.

### Hireology

Rows use the path segment of the career site as ``slug`` and
``https://careers.hireology.com/{slug}/`` as ``url``.

Validation:

1. Call ``https://api.hireology.com/v2/public/careers/{slug}``. It answers
   without a token. A tenant that is gone answers 404: remove the row. The
   career site itself does not say so (it can redirect to the employer's
   own site), and since its 2026 rebuild it carries no data block or token.
2. Run the scraper. A tenant with no open job answers ``count: 0`` and
   stays.
3. A group's site lists the jobs of its member sites, each of which can
   have its own row: ``career_site_url`` of a job then points at the member.
   The job id is the same on both, and the pipeline publishes the job once.
   Count distinct job ids, not listed jobs, when sizing a catalogue change.
4. ``created_at`` is when the job was created, not when it was last
   published: a job that has been open for years keeps its first date.

### ClearCompany

Rows use the career site name as ``slug`` and
``https://apply.clearcompany.com/{slug}`` as ``url``. The earlier
``{slug}.hrmdirect.com`` addresses redirect there.

Validation:

1. Fetch the career site. Require HTTP 200 and a site id
   (``data-site-id``). A site that is gone answers 404 ("This careers page
   isn't available right now").
2. Run the scraper's full walk and require at least one posting.
3. A customer can have several career sites that list the same postings (a
   group site and its brand sites). A posting has the same id on each and is
   published once. Add a second site of a customer only when it lists
   postings the first does not: compare the posting ids, and leave out a
   site when 90% or more of its postings are on a larger site (the few
   that differ are postings opened or closed between the two reads).
4. The row name is the career site's own name (its page title without the
   `` – Careers`` suffix). Each posting names its employer (``brandName``),
   which is what is published; the row name is the fallback.
5. ``{tenant}.clearcompany.com`` is the application portal. Its host label
   is not always the career site name, so do not derive a row from it.

### PeopleAdmin

Rows use the portal's host label as ``slug`` and
``https://{slug}.peopleadmin.com`` as ``url``. Portals served on an
institution's own domain are not covered.

Validation:

1. Fetch ``/postings/search``. Require HTTP 200 and the search page (a
   result count and the list). Hosts under ``ted.peopleadmin.com`` are
   school-district sites that moved to another product, not portals.
2. Leave out the copies an institution keeps beside its live portal:
   labels ending in ``-sb``, ``-training``, ``-test`` and the like. They
   list invented or copied postings.
3. Run the scraper's full walk and require at least one row.
4. Require a posting opened in the 90 days before validation: the newest
   ``Open Date`` (or ``Job Open Date``, ``Posting Date``) of the postings
   that is not in the future, or, when the portal prints no such date, the
   newest ``published`` date of its Atom feed
   (``/postings/all_jobs.atom``). An institution that moved to another
   system often leaves its old portal online with its last postings, test
   postings included.
5. The list holds postings and applicant pools (``/pools/{id}``, standing
   recruitments such as adjunct or tutor pools). Both are published;
   ``raw["kind"]`` tells them apart.
6. The row name is the institution's name as the portal states it (page
   title, footer or feed title), not the host label. A portal can serve one
   part of an institution (student employment, academic positions): name
   the institution.
7. The Atom feed is evidence for rule 4 only, not a source of postings: on
   the portals compared it was a subset of the search list, and stale on
   some.

### Cornerstone

Validation must prove both URL validity and name quality:

1. Use a real tenant-specific careers endpoint, not a generic search or login.
2. Confirm the scraper can fetch jobs from the row.
3. Use the CSV `name` as the human-readable company name when the provider
   exposes only technical slugs.
4. Reject rows where the final company name would be an unreadable slug unless
   a reliable name source is found.

A tenant can run several career sites (``/careersite/{n}/``) that share one
requisition numbering. An extra site is listed when it shows at least one
requisition that no listed site of the tenant shows and its newest posting is
under a year old. Its row carries the tenant's existing `name`, so that the
pipeline's `(company, ats_id)` key keeps one copy of a requisition shown on
several sites. A site that shows test requisitions, or whose titles are mostly
unfilled in the culture it opens in, is left out.

### Radancy TalentBrew

Rows use the career-site origin on the employer's own domain
(``https://jobs.example.com``) as ``url`` and the host as ``slug``.

Validation:

1. Call ``https://{host}/search-jobs/results`` with the site's full search
   parameter set (see the scraper) and ``CurrentPage=1``.
2. Require HTTP 200 and a JSON object whose ``results`` HTML carries a
   numeric ``data-total-results`` above zero.
3. Require at least one ``<a href="/job/.../{org}/{id}" data-job-id="{id}">``
   row (the path may start with a locale, ``/en/job/...`` or
   ``/fr-fr/job/...``), and a schema.org ``JobPosting`` JSON-LD block on
   that posting's page.
4. Record the announced total and run the scraper's full walk. It must
   collect at least 60% of ``min(total, 10000)`` distinct postings (the
   announced total counts a posting once per location). A search returns
   nothing past 10,000 postings: a site above that size is read up to the
   limit and the scraper logs the announced total, so note it in the PR.
5. Reject hosts that are an alias of a site already listed. Posting ids are
   scoped to a host, so an equal total and first id prove nothing: compare
   several full posting paths (``/job/{city}/{title}/{org}/{id}``) and the
   title plus location of the postings.
6. TalentBrew sits in front of the employer's ATS. Skip an employer whose
   ATS board is already listed in another provider file, unless the two
   list different postings (say so in the PR): listing both would publish
   the same postings twice.

### Talentio

Rows use the employer's recruitment home
``https://open.talentio.com/r/1/c/{namespace}/homes/{id}`` (or the same path on
``recruit.talentio.co.jp``) as ``url`` and the employer's name as ``name``.
One employer can have several homes; each is a row.

Validation:

1. Run the scraper. The home must answer for its own namespace and home id,
   and each job's published URL must belong to that namespace.
2. Add a home only when it lists at least one job.
3. Keep a listed home that has no job today.
4. Remove a home that answers 404 on two checks.
5. A job shown on several homes of one employer has one page id; the pipeline
   publishes a page id once.

### Talentsoft (Cegid)

Rows use the career site's host label as ``slug`` and
``https://{slug}.talent-soft.com`` as ``url``. Career sites served on the
customer's own domain are not covered.

Validation:

1. Fetch ``/offre-de-emploi/liste-toutes-offres.aspx?mode=list`` (the path
   is the same on sites in every language). Require HTTP 200 and the offer
   list. A site that sends visitors to its log-in page is an internal
   mobility site: leave it out.
2. Run the scraper's full walk and require at least one offer. The list
   stops at 1,000 offers. A site that announces more is read through one of
   its search filters, one value at a time, when the values of a filter add
   up to the whole list and each holds 1,000 offers at most. Require the
   number of offers read to equal the number announced. A site with no such
   filter is refused by the scraper and cannot be a row.
3. Leave out the copies a customer keeps beside its live site (labels with
   ``test``, ``recette``, ``preprod`` and the like) and the sites for its
   own staff (``-employee``, ``mobilite``): they list invented offers, or
   offers that are not open to outside candidates.
4. A customer can have several sites that list the same offers (an old and
   a new host name, a group site and a brand site). Compare the offer
   numbers and references, and leave out a site when 90% or more of its
   offers are on a larger site.
5. Require an offer dated within the 90 days before validation (the date
   the list shows beside each offer, which is the date the offer was last
   published or updated). A customer that left the product can leave its
   site online with its last offers.
6. Leave out job boards that list offers of many employers without naming
   the employer of each offer in a field the scraper reads, and the State's
   own job board (``place-ep-recrute``, "Choisir le Service Public"), which
   is a government board.
7. The row name is the employer's name as the site states it: the page
   title of the offer list, before " - Résultat de votre recherche". When
   that title is empty or is a label of the site rather than the employer
   ("CANDIDAT ...", "Website ..."), take the name from the site's home page
   or from an offer page, as it is written there.

### Taleo Enterprise

Taleo Enterprise Edition (``{zone}.taleo.net``) is a different product from
Taleo Business Edition (``*.tbe.taleo.net``, the ``taleo`` provider above).

Rows use ``{zone}.taleo.net/{section}`` as ``slug`` and the section's search
page (``https://{zone}.taleo.net/careersection/{section}/jobsearch.ftl``) as
``url``. A zone can have several rows, one per career section.

Validation:

1. Fetch the search page. Require HTTP 200 and a portal number
   (``portal=NNN``). Pages titled ``Career Section Unavailable``,
   ``Privacy Agreement`` or ``Zone Maintenance`` have none: reject them.
2. Run the scraper's full walk and require at least one posting. Do not
   validate on the announced ``totalCount``: it is often larger than the
   number of postings the pages hold, and a section can announce postings
   and list none.
3. Keep external sections only. Reject sections for employees (names such
   as ``in``, ``int``, ``internal``) and sections whose search page asks to
   sign in.
4. A requisition listed in several sections of a zone is published once
   (``ats_id`` is ``{zone}:{requisition number}``). Add a second section of
   a zone only when it lists postings the first does not; a section that
   repeats another adds requests and nothing else.
5. A zone belongs to one Taleo customer, and its sections can still list
   different employers (``edmonton.taleo.net``: the city in one section, the
   public library in another). Give a section its own employer name only
   when it shares no requisition with the other sections of the zone.
   Sections that share requisitions carry one name, the zone customer's
   (``textron.taleo.net``: the ``kautex`` section lists 57 postings that the
   ``textron`` section also lists, so both rows are named Textron). A shared
   requisition is published once, under the name of whichever section is
   read first, so two names for it would make the published employer depend
   on the order of the run. A zone with more than one name is listed in
   ``tests/test_taleo_enterprise_contracts.py``; add it there after
   comparing the requisition numbers of its sections.
6. Reject a zone that is an alias of one already listed (same postings
   under two host names, for example ``tas-acme.taleo.net`` and
   ``acme.taleo.net``): compare requisition numbers and titles.
7. The search page title is usually just ``Job Search``. Take the employer
   name from the detail page title or the postings themselves, and check it
   by hand; never use the zone label (``aa123``) as the name.

### Avature

Validation:

1. Preserve full URL slug casing when the tenant requires it.
2. Validate against the actual Avature listing endpoint or public jobs page
   consumed by the scraper.
3. Reject case-normalized URLs that redirect or fetch a different tenant.

### Wantedly

Rows use `name,slug,url`: the employer's name as the site gives it, the
company's slug as written in its page address (capitals included) and
`https://www.wantedly.com/companies/{slug}`.

- Slugs come from the site's sitemap
  (`https://www.wantedly.com/sitemaps/sitemap.xml.gz`), which lists a
  `/companies/{slug}/projects` page per company with the date it last changed.
  A list unchanged for years shows no open project: start with the most
  recently changed ones.
- Run the scraper on each slug and add a company only when it lists at least
  one project. Take `name` from the listing (`company.name`).
- Ask slowly: the site answers 403 to a client that sends more than a few
  requests per second. Two companies at a time is enough.
- A slug the site no longer knows answers 500 on every request: remove the row.
- Count jobs, not projects. Employers republish one opening as several
  projects, each under a new headline and often tagged with another
  prefecture. The scraper returns projects with the same role label
  (`looking_for`) and the same text as one job. When reporting what a
  catalogue brings, give both figures.
- A company that announces more than 1,000 projects cannot be read to the
  end (100 pages of 10) and is refused. Those seen so far republish one
  advert thousands of times; leave them out.
- The listing also returns the names of staff members. Do not read or keep
  them.

### Varbi

- Use `name,slug,url` with a single employer tenant and
  `https://{slug}.varbi.com/`; never use the aggregate Varbi marketplace.
- Validate `/what:rssfeed/` with the actual scraper, including detail
  enrichment. The scraper never requests the board URL of the catalogue row:
  open it separately and check that it shows the same employer. RSS titles can
  use a different language from the linked detail page; verify the detail's
  `og:url` identity instead.
- A withdrawn advert still answers 200 with its details. It carries a notice
  beside the heading (`.page-header .text-danger`) and is not a job.
- Deduplicate translated items by the numeric job ID within the employer.
- Reject scholarship-only advertisements, expressions of interest, obsolete
  feeds and aliases. A working RSS endpoint is not proof of an active employer.
- Preserve unknown metadata as unknown. Date-only application deadlines belong
  in `raw.application_deadline_date`, not a fabricated midnight timestamp.
- Review cross-source overlap with public employment services before claiming
  that the direct-source vacancies are net-new jobs.

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
