# Listicle extraction benchmark

Trend instrument for listicle-based job-source extraction: measures
name-similarity recall of extracted company names against a hand-labelled
expected set, over a labelled fixture of listicle/table sites. See
`benchmarks/README.md` for the shared conventions (hand-label
denominators, unscored buckets, transport-error handling).

## How to run

```bash
uv run --extra dev pytest benchmarks/listicle/test_listicle_benchmark_matcher.py -v

# full extraction run against the live fixture sites:
uv run python benchmarks/listicle/run_benchmark.py --help
```

- `run_benchmark.py` — the benchmark runner.
- `derive_eu_ground_truth.py` — regenerates the ground-truth labels.
- `data/` — fixtures and expected labels.
- Sibling benchmark dirs: `benchmarks/reach/` (posting reach),
  `benchmarks/pagination/` (next-page detection).

Rates are trend instruments only — never accuracy-bar gates and never
scheduling gates.
