# Design decisions

The decisions that shape the system, and the reason for each.

## Local-first, personal tool

The tool runs on one machine for one person. It is a portfolio and
experimentation project, not a hosted service: scraping at service scale brings
legal and ethical risk that a personal tool does not. All data, including the
CV, stays in a local SQLite database.

## Employer pages over job boards

The source of truth for a posting is the employer's own open-positions page. A
job board (Greenhouse, Lever, Workday, …) is used only when the employer's
verified careers page links to it. A board found through search is treated as a
sign that discovery went sideways, not as a success.

Why: aggregated boards contain stale and duplicated postings, and a board
cannot prove that the employer still uses it. Following the employer's own link
keeps provenance clear. Every stored job records the URL it came from and how
that URL was reached.

## A human gate before any scraping

Discovery produces *candidates*. A person accepts or rejects each company,
singly or in a batch. Only accepted companies are scraped. No run, endpoint or
LLM call can accept a company or a URL.

Why: discovery is noisy (rankings list industries, articles list partners). A
quick human check is cheaper than scraping and scoring the wrong companies.

## Deterministic code first, LLM where it adds value

Code owns URL handling, filters, deduplication, HTTP checks, provenance and the
approval gates. The LLM helps where rules break down:

- extracting company names from text it was shown (never from memory),
- authoring scraping recipes for unknown page structures,
- answering "is anything here a real job posting?",
- extracting a structured profile from a posting,
- drafting tailored CV text.

Model output is validated against a schema, cached, and treated as a
recommendation. The LLM never writes a field that other code derives.

## A search run gathers, a match run judges

Scraping stores postings and facts about them. Scoring is a separate,
explicit run. Filter rules record *flags* on a posting (for example "title says
senior"); a flag lowers the rank but never hides or deletes the posting. The
only way to remove a posting is a human delete, which also remembers its
fingerprint so the next scrape does not add it back.

Why: a filter that silently drops postings loses real jobs, and the loss is
invisible. Flags keep every decision reversible and visible.

## Cheap ranking before paid extraction

Scoring a profiled job is free; extracting the profile costs an LLM call. A
free prefit step ranks unprofiled jobs (target country, role match, skill
density, real-posting signals), and only the top N per run are extracted. Every
match run then rescores every profiled job, so a profile change never needs a
new extraction.

## Drop non-postings at scrape time, and record every drop

Many careers pages link to press releases, service pages and culture pages. The
generic scraper drops links that fail the shared posting check before it
fetches their detail pages. Every drop is recorded with a reason and examples
and shown in the UI, so a wrong drop is visible. A scrape that keeps nothing
ends as `zero_yield` and does not promote the company.

## Politeness

One rate-limited HTTP client (`PoliteClient`) serves all fetches, with per-host
delays. Page counts are capped. Each company costs at most one paid search
request. Runs are started by hand; there is no scheduler.

## Measurement over intuition

Each pipeline stage has a benchmark with hand-labelled cases. A change that
does not move its stage's rate is reverted. Rates report their unscored cases
next to them, and an empty denominator gives "no number", not zero.
Benchmarks show trends; they are not pass/fail gates.

## Out of scope

- Sites behind a login.
- Automatic acceptance of companies or URLs.
- Scheduled, unattended runs.
