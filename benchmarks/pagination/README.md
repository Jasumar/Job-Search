# Pagination benchmark

Legacy: retired once the funnel's company_discovery and position_extraction carry its cases.

## What this measures

An employer careers page is the source of truth for its own listings That makes **pagination the difference
between seeing a company's openings and seeing the ones past page one.** This
benchmark builds the traversal that does not exist yet and scores how honestly it
reports where it stopped.

## Why this was not measurement-only

When this work started there was no pagination in the employer-page path. `scrape_company`
(`ingest/scrapers/pipeline.py:40`) fetches one URL and dispatches once;
`CareersPageScraper.scrape_listings` (`ingest/scrapers/careers_page.py:38`)
walks one HTML document. The only pagination in the repo lived in the ATS
adapters (`ats_json.py`, `oracle_hcm.py`), which were later removed.

A harness measuring "how well do we paginate?" against code that does not
paginate returns a meaningless zero. So the crawler ships first and the
benchmark scores it.

## Loop safety — four independent stops

`crawl_careers_pages` (`ingest/scrapers/pagination.py:442`) cannot loop forever
because termination is enforced four times over, any one of which ends it:

| stop | meaning | clean? |
|---|---|---|
| `no_signal` | no affordance on the page | yes |
| `signal_disabled` | affordance present but disabled/self-referential | yes |
| `offsite` | next link leaves the employer's careers section | yes |
| `repeat` | next URL already visited | yes |
| `no_new_jobs` | page yielded zero new job URLs | yes |
| `page_cap` | hit the `--max-pages` cap | **no — truncated** |
| `fetch_error` | network/parse failure | unknown |
| `load_more_unsupported` | in-page JS control, not crawlable by URL | no — unbounded |

`same_careers_scope` (`pagination.py:392`) enforces the host/scheme/path-prefix
guard. Any crawler that follows an arbitrary "Next" link will eventually leave
the careers section and wander into `/blog` or an unrelated domain; that is the
failure mode the standing decision makes unacceptable.

## Bug found and fixed en route

`_JOB_LINK_PATTERNS` (`careers_page.py:14`) matches `/jobs?page=2`, so a "Next"
link was scraped as a *job* titled "Next" — polluting the DB on every paginated
employer page. `careers_page.py` now skips any anchor `is_pagination_link`
recognises. Test: `test_pager_link_is_never_scraped_as_a_job`.

A second one, found by the benchmark itself: a bare page-number link (`<a
href="?page=2">2</a>`) was read as a next signal, so the crawl took a redundant
step and reported `repeat` when the truth was `signal_disabled`. A bare number
can only ever be the page you are already on. Test:
`test_django_pager_crawl_stops_with_signal_disabled_not_repeat`.

## What the harness reports, and why not a rate

Employer careers pages have **no free total**. There is no "of 47 listings" to
compute coverage against, so recall is unmeasurable here and the harness does
not pretend otherwise. Per site it reports pages crawled, jobs, which
termination signal fired, and whether the stop was clean. Aggregate:

- `clean_termination_rate` — share of *scored* sites that both found a signal
  and terminated cleanly. This is the headline number.
- `full_coverage_rate` — share of scored sites that terminated cleanly on a
  non-truncated stop, i.e. nothing was left uncrawled for budget reasons.
- `unknown` sites (fetch errors) are **excluded from both rates**, never
  counted as failures.

Benchmarks have no accuracy bars and
gate nothing. These numbers are for diagnosing the crawler, not for pass/fail.

## Fixture reality — read this before trusting a rate

`data/sites.json` records, per site, what was actually observed on 2026-10-02.
The honest summary: **server-rendered paginated employer careers pages are very
rare in 2026.** Of everything probed, exactly one paginates:

- `python.org/jobs/` — real `?page=N` pager, disabled Prev on page 1. The only
  confirmed multi-page crawl target.
- `revolut.com/en-US/careers` — a load-more control with no URL. Reported as
  `load_more_unsupported`, deliberately *not* counted as clean.
- Mozilla, Airbnb, Zalando, mol.fi — no affordance found at all.

Reachability limits are recorded in `rejected_candidates`: careers.gitlab.com and
careers.tetrate.io fail DNS from this host; careers.bolt.eu, weworkremotely.com
and indeed.com require Playwright.

**Consequence:** a `clean_termination_rate` over 8 sites where 7 have no
pagination at all measures almost nothing. With one paginated site, the rate is
0.0 or 1.0 and nothing in between. The traversal logic is exercised properly by
the 31 unit tests against real-world markup; the live run is a smoke test and a
record of what employers are actually shipping, not a measure of the crawler.

`python.org/jobs/` is a community jobs board, not an employer hiring page. It is
included precisely because it paginates. Under the standing decision it would
never be a discovery source, so no conclusion about employer coverage should be
drawn from it.

## Decisions taken without confirmation

Four questions were put to the user before building and went unanswered, so the
recommended options were taken and are recorded here rather than in the plan
docs (agents do not edit plan files).

1. **Build the crawler and the harness**, not harness-only — measurement against
   a non-paginating crawler is a meaningless zero.
2. **The pagination harness is standalone.** The shared-seam prerequisite
   (generalise the runner once on main before parallel work starts) was not
   applied, but `benchmarks/reach/` already existed with its own runner. The seam is *not* extracted here; reconciling the two
   runners is a merge-time decision for the primary agent.
3. **Fixture sites proposed and verified by the agent**, not supplied by the
   user. `sites.json` is the proposal, with the probe evidence attached.
4. **Clean-termination rate + per-site detail, no recall figure** — recall needs
   a free total that employer pages do not provide.

## Running it

```sh
uv run python benchmarks/pagination/run_pagination_bench.py --list-sites
uv run python benchmarks/pagination/run_pagination_bench.py
uv run python benchmarks/pagination/run_pagination_bench.py --only python_org_jobs
uv run python benchmarks/pagination/run_pagination_bench.py --max-pages 3
```

Writes `reports/pagination/last_report.json` (git-ignored) plus a console table.
Use `--report PATH` to write elsewhere. Uses `PoliteClient` for rate limiting;
`--sites` points at an alternative fixture file.

Tests: `tests/test_pagination.py` (31, traversal + loop safety, offline) and
`tests/test_pagination_bench_harness.py` (10, the scoring rules that decide the
headline number, offline). The harness's verdict logic is unit-tested precisely
because the live run needs the network and cannot run in CI.