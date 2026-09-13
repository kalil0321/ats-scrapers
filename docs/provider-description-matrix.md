# Provider Description Matrix

This table covers the 52 providers listed below.
`API/feed` means the data comes from a machine-readable endpoint such as JSON,
XML, RSS, GraphQL, or a markdown endpoint. `HTML` means the scraper parses a
page/rendered page rather than a structured job payload.

| Provider | Has description? | Is description 2 steps? | Job fetch: API / HTML? | Description: API / HTML? |
|---|---:|---:|---|---|
| `amazon` | Yes | No | API | API |
| `apple` | Yes | No | API | API |
| `arbetsformedlingen` | Yes | No | API | API |
| `ashby` | Yes | No | API | API |
| `avature` | Yes | Yes | HTML | HTML |
| `bamboohr` | Yes | Yes | HTML | API |
| `breezy` | Yes | Yes | API | HTML |
| `builtin` | Yes | No | HTML | HTML |
| `bundesagentur` | Yes | Yes | API | API |
| `cornerstone` | Yes | No | API/feed + HTML bootstrap | API/feed |
| `eightfold` | Yes | Yes | API | API |
| `eures` | Yes | Yes | API/feed | API/feed + detail API fallback |
| `gem` | Yes | Yes | API/feed | API/feed |
| `getonbrd` | Yes | No | API/feed | API/feed |
| `google` | Yes | Yes | HTML | HTML |
| `greenhouse` | Yes | No | API/feed | API/feed |
| `herp` | Yes | Yes | HTML | HTML (JSON-LD) |
| `hrmos` | Yes | No | HTML | HTML |
| `icims` | Yes | Yes | HTML | HTML |
| `jazzhr` | Yes | Yes | HTML | HTML |
| `jobsch` | Yes | Yes | API | HTML |
| `join_com` | Yes | Yes | HTML | HTML |
| `keka` | Yes | No | HTML bootstrap + API | API |
| `lever` | Yes | No | API | API |
| `manfred` | Yes | Yes | API/feed | API/feed |
| `mercor` | Yes | No | API/feed | API/feed |
| `meta` | Yes | Yes | HTML / browser | HTML |
| `oracle` | Yes | Yes | API/feed | API/feed |
| `paycom` | Yes | Yes | HTML bootstrap + API | API |
| `personio` | Yes | Yes | API | HTML |
| `phenom` | Yes | No | API/feed + HTML bootstrap | API/feed |
| `pinpoint` | Yes | No | API/feed | API/feed |
| `programathor` | Yes | Yes | HTML | HTML |
| `recruitee` | Yes | No | API/feed | API/feed |
| `recruiterbox` | Yes | No | API/feed | API/feed |
| `remoteok` | Yes | No | API/feed | API/feed |
| `rippling` | Yes | Yes | API/feed | API/feed |
| `smartrecruiters` | Yes | Yes | API/feed | API/feed |
| `softgarden` | Yes | No | API/feed | API/feed |
| `successfactors` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `taleo` | Yes | Yes | HTML | HTML |
| `teamtailor` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `tesla` | Yes | Yes | API/feed / browser | API/feed |
| `thehub` | Yes | No | API/feed | API/feed |
| `tiktok` | Yes | No | API/feed | API/feed |
| `uber` | Yes | No | API/feed | API/feed |
| `wanted` | Yes | Yes | API/feed | API/feed |
| `wellfound` | Yes | No (browser); Yes (legacy) | Public role-page structured data (local browser) | Embedded full descriptions; Firecrawl legacy opt-in |
| `weworkremotely` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `workable` | Yes | Yes | API/feed | API/feed (Markdown) |
| `workday` | Yes | Yes | API/feed | API/feed |
| `ycombinator` | Yes | No | API/feed | API/feed |

## Wellfound browser readiness

The credential-free backend is experimental, not validated for unattended
production publication. On 2026-09-13, a local browser exposed real structured
jobs and descriptions, but the VPS received HTTP 403. All 23 public
`finance-manager` pages exposed 691 unique job IDs against an advertised total
of 991, before filtering ATS imports. The scraper rejects that incomplete run
instead of publishing it as a complete catalogue. Resolving that coverage gap
and validating access on the deployment host are release gates. Role coverage
is limited to the configured slugs; the confirmed-404 `founders-associate`
slug has been removed.
