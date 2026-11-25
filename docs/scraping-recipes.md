# Scraping recipes: the self-healing scraper experiment

## The question

Can a scraper change itself with LLM help, beyond fixed rules, and still be
trustworthy? Careers sites differ in structure, and they change without notice.
Hand-written code per site does not scale. Sending every page to an LLM on
every run is slow, expensive and unpredictable.

The approach tested here: **the LLM proposes once, code replays and verifies
every time.**

## Life cycle of a recipe

```
Gather information ──► Build recipe ──► Use recipe ──► Self-heal
  capture page HTML,     LLM writes a      replayed by      replay fails or
  rendered DOM, and      typed recipe;     code on every    returns nothing:
  JSON responses         code replays it   run, no LLM      retire the recipe,
        ▲                before storing                     build a new one
        └───────────────────────── rebuild ─────────────────────┘
```

1. **Capture.** One capture keeps every artifact the page produced: static
   HTML, rendered DOM, each JSON/XHR response, and job-board JSON when the page
   links to a board. Code marks which artifact carried the postings.
2. **Author.** The LLM gets a reduced view: candidate container selectors with
   real match counts, a few example subtrees, and a body outline. It does not
   get raw HTML, the number of postings, or sample titles, because those are
   the measurement. A fixed example pulls the model toward copying it.
3. **Validate.** The recipe body is parsed by a typed model. Code, not the LLM,
   sets the host allowlist and the verification data. A body that does not
   parse is discarded. The author gets up to three trials.
4. **Replay.** One generic interpreter runs every recipe. There is no per-site
   code and no LLM call at runtime.
5. **Self-heal.** An empty replay, a schema failure or a confirmed
   disappearance of postings retires the recipe (`stale`), and the generic
   scraper takes over until a new recipe is authored.

## Recipe kinds

- **`api`**: a request (method, URL template, query or body template, headers,
  or a token read from the page), a path to the job array in the response,
  pagination by page, offset or cursor, and a field map.
- **`dom`**: CSS or XPath selectors: a job container, then link, title and
  location inside it. Stable anchors (href patterns, `data-*` attributes,
  semantic roles) are preferred over generated class names. Pagination by
  page, offset or a next link, capped at 20 pages.

## How a recipe is judged

The rules came from measurement. Every deterministic quality filter that was
tried also dropped real postings. A title can contain "legal" or "privacy".

- **A replay is accepted on one rule: it returned postings.** URL shape, title
  match, count and overlap with a reference list are recorded as signals,
  never used to reject.
- **Structural faults reject.** A list path that does not resolve, a second
  page identical to the first, or an empty result.
- **Statistical oddities route to a check.** Duplicate titles, empty dates or
  odd URLs send the replay to the **posting-existence gate**: a model is shown
  up to 15 items, the worst ones first, and answers one question: is at least
  one of these a real job posting? On "none", the recipe is flagged and the run
  falls back to the generic scraper. A flag never deletes stored postings.
- **Quality is agreement with an independent extraction.** The reference list
  is taken from the capture itself (site JSON, IDs joined to page links, or a
  posting-shaped link cluster). It is measured on each run, so drift shows up
  as a URL difference between two runs.

The guiding rule: a check that drops a real posting is worse than a check that
misses a bad recipe, because a bad recipe can still be caught by the gate.

## Where recipes run today

- **Pasted listing pages** (`name_collect`): a recipe extracts company names
  from rankings and articles. Text-only items (no link) are supported.
- **Careers pages**: a scrape run replays a company's verified recipe in place
  of the generic scraper. Recipes for careers pages are authored in the
  benchmark harness (`benchmarks/recipe_author/`); there is no production
  authoring trigger for them yet.

## Results so far

Measured on 25 hand-labelled careers pages:

- 12 of 25 pages produced a replayable recipe. 9 were capture gaps (the
  capture did not contain the posting list in a form the detectors found),
  3 failed and 1 raised an error. Working estimate: 40–60% of sites can be
  understood with the current capture.
- Replay recall against the reference was 47% overall and 94% when two sites
  that returned navigation links are excluded. Those two sites are the reason
  the posting-existence gate exists.

## Not built

- Recipes keyed by host, so one recipe serves every company on the same
  careers-site template.
- Automatic recipe authoring for careers pages in production.
- Dates, descriptions and detail pages in `dom` recipes (only pagination is
  supported).
