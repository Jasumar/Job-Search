# Benchmarks

Measurement harnesses for the scraping and discovery pipeline. They show
trends; they are not pass/fail gates. A change that does not move its stage's
rate is reverted.

## The funnel

One funnel over the scraping chain, measured in two parts with separate
fixtures. Each stage is a rate over the previous one, and every drop-off is
named.

**position_extraction** (`funnel/position_extraction/`), per company in
`funnel/data/companies.json` (25 companies: 13 large, 5 medium, 4 startups,
3 Finnish SMEs):

- A1 company → careers URL
- A2 careers page → open-positions page (a job board counts only when reached
  through a link on the employer's careers page)
- A3 open-positions page → postings
- A4 postings → next page

| Script | Measures |
|---|---|
| `run.py` | per-stage rates, end to end |
| `entry_run.py` | open-positions page resolution against the labels |
| `seeded_run.py` | posting extraction when seeded with the labelled careers URL |

**company_discovery** (`funnel/company_discovery/`):

- B1 search query → company found in the top 10 results (`score_b1.py`,
  queries in `funnel/data/queries.json`)
- B2 mention page (ranking, article, table) → company names (`score_b2.py`,
  pages in `funnel/data/mention_pages.json`)

Both read frozen snapshots, so changes in live search results cannot appear as
regressions. **The snapshots are not in this repository**, because they are
copies of third-party pages. Create your own with:

```bash
uv run python benchmarks/funnel/company_discovery/snapshot.py
```

`last_report_*.json` files hold the numbers from the original snapshot.

## Other harnesses

| Directory | Measures |
|---|---|
| `recipe_author/` | LLM recipe authoring and replay on the labelled careers pages; `report-*.md` are the generated results |
| `listicle/` | company-name extraction from listicle pages |
| `reach/` | whether a posting's detail page can be fetched (legacy) |
| `pagination/` | next-page detection (legacy) |

## Conventions

- Rates use hand-labelled denominators. A case counts only when a label says
  the entry can judge it. Everything else is counted and named in an
  "unscored" bucket.
- An empty denominator gives `None`, not `0.0`.
- Transport errors are left out of the rate and printed beside it.
- A rate is reported only together with its unscored count.
- Hand labels are made by a person. Proposed labels are excluded until checked.
- Sites behind a login are out of scope.

Current numbers: [docs/status-and-limitations.md](../docs/status-and-limitations.md).
