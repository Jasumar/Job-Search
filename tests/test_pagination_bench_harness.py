"""Harness scoring logic, tested offline against synthetic crawl results.

The live run in ``run_pagination_bench.py`` talks to the network, so the scoring
rules that decide the headline number are tested here instead. A benchmark whose
verdict logic is untested is just a script that prints numbers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

BENCH_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "pagination" / "run_pagination_bench.py"
spec = importlib.util.spec_from_file_location("run_pagination_bench", BENCH_PATH)
assert spec is not None and spec.loader is not None
bench: ModuleType = importlib.util.module_from_spec(spec)
# dataclass resolves annotations through sys.modules[cls.__module__], so the
# module has to be registered before it is executed, not after.
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


def _result(reason: str, detail: str = "", pages: int = 1, jobs: int = 1):
    """A minimal stand-in for a CrawlResult."""
    return SimpleNamespace(
        stop_reason=reason,
        stop_detail=detail,
        pages_crawled=pages,
        pages=["u"] * pages,
        jobs=[object()] * jobs,
    )


def test_clean_reasons_score_as_clean():
    for reason in ("no_signal", "signal_disabled", "offsite", "repeat", "no_new_jobs"):
        label, verdict, _ = bench.classify(_result(reason))
        assert label == "determinate"
        assert verdict == "clean", reason


def test_page_cap_is_truncated_not_clean():
    label, verdict, notes = bench.classify(_result("page_cap"), max_pages=10)
    assert label == "determinate"
    assert verdict == "truncated"
    assert any("10-page cap" in n for n in notes)


def test_fetch_error_is_unknown_and_never_counted():
    label, verdict, _ = bench.classify(_result("fetch_error", "timeout"))
    assert label == "unknown"
    assert verdict == "unknown"


def test_load_more_is_unbounded_not_clean():
    """A load-more control means the rest of the listings was never fetched.

    Scoring it clean would credit a partial crawl with full coverage.
    """
    label, verdict, notes = bench.classify(_result("load_more_unsupported", "Show more"))
    assert label == "determinate"
    assert verdict == "unbounded"
    assert any("Show more" in n for n in notes)


def test_unhandled_reason_is_reported_as_unbounded():
    label, verdict, notes = bench.classify(_result("wat"))
    assert label == "determinate"
    assert verdict == "unbounded"
    assert notes


def test_unknown_sites_are_excluded_from_rates():
    report = bench.Report()
    report.add(_site("a", "clean", "determinate"))
    report.add(_site("b", "unknown", "unknown"))
    assert report.clean_termination_rate == 1.0
    assert report.to_dict()["totals"]["scored"] == 1
    assert report.to_dict()["totals"]["unknown"] == 1


def test_truncated_counts_toward_clean_termination_but_not_full_coverage():
    report = bench.Report()
    report.add(_site("a", "clean", "determinate"))
    report.add(_site("b", "truncated", "determinate"))
    assert report.clean_termination_rate == 1.0
    assert report.full_coverage_rate == 0.5


def test_unbounded_lowers_clean_termination_rate():
    report = bench.Report()
    report.add(_site("a", "clean", "determinate"))
    report.add(_site("b", "unbounded", "determinate"))
    assert report.clean_termination_rate == 0.5


def test_rates_are_none_with_no_scored_sites():
    report = bench.Report()
    assert report.clean_termination_rate is None
    assert report.full_coverage_rate is None


def _site(key: str, verdict: str, label: str) -> bench.SiteResult:
    return bench.SiteResult(
        key=key,
        url=f"https://example.invalid/{key}",
        pages_crawled=1,
        jobs=1,
        stop_reason="x",
        stop_detail="",
        label=label,
        verdict=verdict,
    )
