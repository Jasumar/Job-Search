# Status and limitations

## What works

The full pipeline runs end to end, through the web app and the CLI: paste a
listing page or search, review companies, resolve their open-positions pages,
scrape postings, and see them ranked against your CV. Development is paused
after this end-to-end demo.

## Measured numbers

Benchmarks run on 25 hand-labelled companies (large and medium corporates,
startups and Finnish SMEs) and on frozen search and page snapshots. Rates use
hand-labelled denominators.

| Instrument | Result |
|---|---|
| Careers page → open-positions page | 19 of 25 |
| Open-positions page → correct postings | 11 of 19 |
| End to end (company → postings) | 6 of 25 (24%) |
| Entry resolution | a page reached for 23 of 24; 11 match the label; 10 with at least one real posting |
| Search → company found | 30 of 65 (46%); hardest query family 1 of 21 |
| Pasted page → company names | 24 of 57 labelled names (42%); 13 of 15 pages give names |

The weak stages are upstream of posting extraction:

- **Choosing the right listing page.** Landing pages carry many navigation
  links, and a page can win on link count alone. The resolver now counts only
  links that look like real postings, which moved some cases, but many landing
  pages still have no posting signal.
- **Reading company names from articles and tables.** Structured lists and
  tables now work well (98 of 100 on a market-cap ranking). Prose and
  industry lists remain weak.

Posting extraction on the right page works well. The LLM recipe results are in
[scraping-recipes.md](scraping-recipes.md).

## Known limitations

- **Serial runs.** One worker thread and one rate-limited client. A careers
  search over 1,000 companies takes roughly 4–11 hours.
- **Match thresholds are placeholders.** The "worth applying" (0.70) and
  "consider" (0.40) bands are not yet tuned against a real score distribution.
- **Detail-page failures lose the posting.** A posting whose detail page is
  JavaScript-only or fails to load is not stored.
- **Pagination judgement.** The scrape filter judges page-2 links against the
  first page's layout.
- **No staleness check.** A dead careers URL keeps being retried; nothing marks
  postings as closed.
- **No company deduplication across runs** beyond known-company suppression.
- **Login walls** are out of scope.
- **A job with no stated requirements** and an unknown role is scored on
  location alone, so it can score high.
