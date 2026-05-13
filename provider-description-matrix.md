# Provider Description Matrix

This table covers the 47 providers configured in `scripts/run_pipeline.py`.
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
| `eures` | Yes | No | API/feed | API/feed |
| `gem` | Yes | Yes | API/feed | API/feed |
| `getonbrd` | Yes | No | API/feed | API/feed |
| `google` | Yes | Yes | HTML | HTML |
| `greenhouse` | Yes | No | API/feed | API/feed |
| `icims` | Yes | Yes | HTML | HTML |
| `jazzhr` | Yes | Yes | HTML | HTML |
| `jobsch` | Yes | Yes | API | HTML |
| `join_com` | Yes | Yes | HTML | HTML |
| `lever` | Yes | No | API | API |
| `manfred` | Yes | Yes | API/feed | API/feed |
| `mercor` | Yes | No | API/feed | API/feed |
| `meta` | Yes | Yes | HTML / browser | HTML |
| `oracle` | Yes | Yes | API/feed | API/feed |
| `personio` | Yes | Yes | API | HTML |
| `phenom` | Yes | No | API/feed + HTML bootstrap | API/feed |
| `pinpoint` | Yes | No | API/feed | API/feed |
| `programathor` | Yes | Yes | HTML | HTML |
| `recruitee` | Yes | No | API/feed | API/feed |
| `recruiterbox` | Yes | No | API/feed | API/feed |
| `remoteok` | Yes | No | API/feed | API/feed |
| `rippling` | Yes | Yes | API/feed | API/feed |
| `smartrecruiters` | Yes | Yes | API/feed | API/feed |
| `successfactors` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `taleo` | Yes | Yes | HTML | HTML |
| `teamtailor` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `tesla` | Yes | Yes | API/feed / browser | API/feed |
| `thehub` | Yes | No | API/feed | API/feed |
| `tiktok` | Yes | No | API/feed | API/feed |
| `uber` | Yes | No | API/feed | API/feed |
| `wanted` | Yes | Yes | API/feed | API/feed |
| `wellfound` | Yes | Yes | HTML / Firecrawl | HTML / Firecrawl |
| `weworkremotely` | Yes | No | API/feed (RSS/XML) | API/feed (RSS/XML) |
| `workable` | Yes | Yes | API/feed | API/feed (Markdown) |
| `workday` | Yes | Yes | API/feed | API/feed |
| `ycombinator` | Yes | No | API/feed | API/feed |
