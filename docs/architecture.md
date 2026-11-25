# Architecture

What exists, where it lives, and which parts are load-bearing.

## Processes

```
Browser ── React app (apps/web)
              │  /api (Vite proxy in development)
              ▼
           FastAPI (apps/api) ── reads results, enqueues runs; runs no pipeline code
              │
              ▼
           SQLite (data/jobs.db, WAL) ◄── job_runs queue
              ▲
              │  claims one run at a time
           Worker (apps/worker) ── executes discover / scrape / match / … runs
              │
              ├── OpenRouter (LLM)
              ├── Search API (Brave, DuckDuckGo or SearXNG)
              └── Employer careers sites (PoliteClient: rate-limited HTTP, Playwright tier)
```

All Python code shares one package, `jobsearch_core` (`packages/core`), and one
uv workspace virtual environment. The CLI (`apps/cli`) calls the same core
functions as the worker.

## Pipeline stages

| Stage | Command | Core modules | What it does |
|---|---|---|---|
| Discover | `discover` | `search/discover.py`, `search/resolver.py`, `search/templates.py`, `search/careers_search.py` | Finds company identities, then each company's careers page; stops at the review gate |
| Verify | `verify` | `company_source.py`, review endpoints | A human accepts or rejects candidates; only accepted companies are scraped |
| Scrape | `scrape` | `ingest/scrapers/pipeline.py`, `ingest/site_class.py`, `ingest/scrapers/entry_resolution.py`, `ingest/scrapers/sitemap.py`, `ingest/recipe_replay.py`, `ingest/listing.py`, `ingest/scrapers/posting_check.py` | Resolves the open-positions page, collects postings, drops non-postings, normalizes and stores them |
| Match | `match` | `scoring/prefit.py`, `scoring/match.py`, `scoring/profile.py`, `role_taxonomy.py`, `countries.py`, `run.py` | Ranks unprofiled jobs for free, extracts profiles for the top N with the LLM, scores every profiled job against the CV evidence |
| Tailor | `tailor` | `tailor/workflow.py` | Evidence-backed CV variants behind an approval gate |

### Two ways to find companies

- **Search-driven** (`discover`): query templates per segment and country
  produce search results; the LLM extracts company identities only from text it
  was shown.
- **Source-driven** (`name_collect`): the user pastes listing pages (rankings,
  articles, tables). Names come from the page structure (link clusters, table
  columns, lists, repeated headings) through a recipe; an LLM reads the page
  text only when no structure is found. All names start as `pending`.

A `careers_search` run then resolves an open-positions page for each selected
company: one paid search request per company, same-origin link descent from
the landing page, and posting checks to choose the right page.

### Scraper strategy (site classes)

A classifier picks the order of strategies for each careers page. It does no
I/O.

| Class | Page type | Strategy |
|---|---|---|
| A | Known job-board backend (Greenhouse, Lever, …) | Typed JSON adapter, paginated |
| B | Embedded structured data | JSON-LD `JobPosting`, then microdata |
| C | Server-rendered HTML | Generic link and heading extraction |
| D | JavaScript-only page | Playwright render, then A/B/C on the rendered DOM; job-list XHR responses are captured |
| F | Unknown structure | LLM-authored recipe, replayed by code ([scraping-recipes.md](scraping-recipes.md)) |

## Data model (SQLite, Alembic)

- `companies`: approved hiring sources. `careers_url` is the durable entry
  point. `open_positions_url` is the resolved listing page, stored with the
  landing page and link text that led to it. `ats_platform` is scrape-time
  metadata, never proof of origin.
- `jobs`: postings with provenance (`authority_url`, `extraction_url`,
  `verification_status`), a structured `job_profile` (`job_group`, `job_role`,
  skills, location, seniority), `total_score` and `fit_components`. Filter hits
  are stored as `fit_flags`: facts about the posting, never a status.
- `job_exclusions`: fingerprints of postings a human deleted, so a later scrape
  cannot add them again.
- `user_profile`: one row; contact fields and search selections (role ids,
  countries, work arrangements). Contact fields never reach scoring prompts.
- `user_evidence`: one row; facts from the CV plus free-text experience,
  written only by an explicit Save.
- `company_recipes`: LLM-authored scrape paths (`api` or `dom` kind), validated
  by one schema (`ingest/recipe.py`), with status `candidate`, `verified` or
  `stale`.
- `recipe_flags`: one row per posting-existence check; history, never
  overwritten.
- `company_xhr_capture`: JSON responses observed during a browser render;
  evidence only.
- `job_runs`: the queue for background runs; at most one active run per type.
- `sources`, `run_sources`, `search_cache`: source-driven runs. Each
  `run_sources` row records what one company or source produced, what was
  dropped and why, and the evidence of the entry descent or recipe authoring.

## Rules that are easy to break

Each rule states an invariant and where it is enforced. Read the code at that
point before changing it.

1. **A job board is never a job source unless the employer's careers page
   links to it.** Enforcement is spread out: `scrape_company(verified=False)`
   refuses before any fetch; `add_company` and the CLI refuse a board URL;
   discovery skips board and aggregator results before fetching; the entry
   descent harvests same-origin links only; `ingest_scraped` rejects jobs
   without verified provenance. Tests: `tests/test_verified_origin.py`.
2. **The LLM writes only fields that no other code derives again.**
   "Deterministic" means that the whole call graph reaches no LLM.
   `scoring/match.py` and `report.py` enforce this.
3. **A run is explicit, and a run rescores everything.** Scoring is free, so
   every match run scores every profiled job. Only the paid profile extraction
   is bounded (`max_profile_extractions`, chosen by the free prefit rank). A
   profile edit never starts a run.
4. **A stored description is not proof that a page is reachable.**
   `fetch_detail` returns `None` or raises. Binary bodies and short login walls
   are refused (`ingest/textguards.py`).
5. **Human approval gates do not move.** No endpoint or run accepts a company,
   a resolved URL or application material automatically.
6. **Budgets are clamped on the server.** Every search request counts against
   `DiscoveryBudget`; a request can lower a cap, never raise it.
7. **Schema changes go through Alembic only**, with a single head.
8. **No lazy imports in worker threads.** A lazy import inside a worker thread
   can deadlock on the Python import lock, so heavy modules are imported at
   module level.
