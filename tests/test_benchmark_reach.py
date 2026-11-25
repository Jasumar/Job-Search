"""Offline tests for the benchmark harness and the reach scorer.

No network, no LLM, no browser. Everything here is a pure function over text
the scorer will see in the wild.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent / "benchmarks"
REACH_DIR = BENCHMARKS_DIR / "reach"
# harness.py sits in benchmarks/, the scorer sits beside its runner in
# benchmarks/reach/. Both directories are needed on the path, which is the same
# two-level insert the runner does.
sys.path.insert(0, str(BENCHMARKS_DIR))
sys.path.insert(0, str(REACH_DIR))

import harness  # noqa: E402
import score_reach  # noqa: E402

# A sibling benchmark entry (benchmarks/listicle/) also ships a module called
# `run_benchmark`. Importing this one by bare name would make the test depend on
# collection order, so load it by path under a unique name instead.
_spec = importlib.util.spec_from_file_location(
    "reach_run_benchmark", REACH_DIR / "run_benchmark.py"
)
assert _spec and _spec.loader
run_benchmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_benchmark
_spec.loader.exec_module(run_benchmark)

from harness import (  # noqa: E402
    CaseResult,
    GroundTruth,
    Report,
    RunContext,
    _parse_verified,
)
from score_reach import (  # noqa: E402
    BINARY_DOCUMENT,
    BLOCKED,
    LOGIN_WALL,
    REACHABLE,
    TOO_THIN,
    TRANSPORT_ERROR,
    UNSCORED_OBSTACLES,
    classify_text,
)

CASES_PATH = REACH_DIR / "data" / "cases.json"


def load_cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


class TestGroundTruthLoading:
    def test_every_row_carries_last_verified(self):
        """The suite rule: every GT row gets a last-verified date."""
        for key, row in load_cases().items():
            assert row.get("last_verified"), f"{key} has no last_verified"

    def test_every_row_carries_a_url(self):
        for key, row in load_cases().items():
            assert row.get("url"), f"{key} has no url"

    def test_labels_are_known_obstacles_or_null(self):
        known = {
            REACHABLE,
            BLOCKED,
            LOGIN_WALL,
            TOO_THIN,
            BINARY_DOCUMENT,
            TRANSPORT_ERROR,
        } | set(UNSCORED_OBSTACLES)
        for key, row in load_cases().items():
            label = row.get("label")
            assert label is None or label in known, f"{key} label {label!r} not in vocabulary"

    def test_loads_rows_with_dates(self):
        gt = GroundTruth.load("posting reach rate", CASES_PATH)
        assert gt.rows, "expected seeded cases"
        row = next(iter(gt.rows.values()))
        assert row.verified_at is not None


class TestStaleness:
    def test_verified_date_parses(self):
        assert _parse_verified("k", "2026-10-02") == "2026-10-02"

    def test_bad_date_raises_benchmark_error(self):
        """A typo'd date must fail at load, naming the row."""
        try:
            _parse_verified("some_row", "02/10/2026")
        except harness.BenchmarkError as exc:
            assert "some_row" in str(exc)
            return
        raise AssertionError("expected BenchmarkError for a non-ISO date")

    def test_bad_date_in_file_fails_at_load(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "cases.json"
            bad.write_text(
                json.dumps({"k": {"url": "https://x.example", "label": None, "last_verified": "nope"}}),
                encoding="utf-8",
            )
            try:
                GroundTruth.load("posting reach rate", bad)
            except harness.BenchmarkError as exc:
                assert "k" in str(exc)
                return
            raise AssertionError("expected load to reject a malformed date")

    def test_missing_date_is_stale(self):
        row = harness.GroundTruthRow(key="k", label=None, last_verified=None)
        assert row.is_stale()

    def test_fresh_date_is_not_stale(self):
        yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        row = harness.GroundTruthRow(key="k", label=None, last_verified=yesterday)
        assert not row.is_stale()

    def test_old_date_is_stale(self):
        row = harness.GroundTruthRow(key="k", label=None, last_verified="2020-01-01")
        assert row.is_stale()

    def test_staleness_lists_offending_keys(self):
        gt = GroundTruth.load("posting reach rate", CASES_PATH)
        stale = gt.staleness()
        assert isinstance(stale, list)


class TestReachScorer:
    def test_full_posting_is_reachable(self):
        body = "We are hiring a backend engineer. " * 20
        html = f"<html><body><main><p>{body}</p></main></body></html>"
        reachable, obstacle = classify_text(html)
        assert reachable, obstacle

    def test_empty_is_too_thin(self):
        reachable, obstacle = classify_text("   ")
        assert not reachable and obstacle == TOO_THIN

    def test_short_page_is_too_thin(self):
        reachable, obstacle = classify_text("<html><body><main><p>Hi</p></main></body></html>")
        assert not reachable and obstacle == TOO_THIN

    def test_cloudflare_challenge_is_blocked(self):
        html = "<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>"
        reachable, obstacle = classify_text(html)
        assert not reachable and obstacle == BLOCKED

    def test_pdf_bytes_are_binary(self):
        reachable, obstacle = classify_text("%PDF-1.7\n%����\n1 0 obj\n<< /Length 99 >>\n")
        assert not reachable and obstacle == "binary_document"

    def test_login_wall_only_when_short(self):
        """A wall REPLACES the posting; an incidental login link does not.

        This is the false positive that shipped in the first version of this
        scorer: Built In's real postings carry "Please log in or sign up to
        report this job" alongside 4-15KB of usable description, and a naive
        marker match called all of them login walls.
        """
        wall = "<html><body>Already registered? Login Email Password Log in with a code</body></html>"
        reachable, obstacle = classify_text(wall)
        assert not reachable and obstacle == LOGIN_WALL

        real = "Great posting body. " * 200 + "Please log in or sign up to report this job."
        reachable, obstacle = classify_text(real)
        assert reachable, f"incidental login link misread as {obstacle}"

    def test_login_wall_threshold_has_margin(self):
        """The real 763-vs-4844 gap means the threshold can move without flipping.

        Both fixtures are asserted to sit on the side of the threshold they claim,
        so this test fails loudly if MIN_USABLE_CHARS or LOGIN_WALL_MAX_CHARS is
        retuned past the margin instead of silently inverting a verdict.
        """
        wall_side = "Already registered? Login Email Password Log in with a code " * 18
        assert 200 < len(wall_side) < 2000, len(wall_side)
        assert classify_text(wall_side)[1] == LOGIN_WALL

        posting_side = "Real posting content here. " * 100
        assert len(posting_side) > 2000, len(posting_side)
        assert classify_text(posting_side)[1] == REACHABLE

    def test_wall_padding_above_threshold_is_a_documented_blind_spot(self):
        """A padded wall would read as reachable. Recorded, not hidden."""
        padded = "Already registered? Login Email Password Log in with a code " * 200  # ~8KB
        reachable, obstacle = classify_text(padded)
        assert reachable, "documented blind spot: length-based test cannot see a padded wall"

    def test_json_payload_is_reachable(self):
        """ATS JSON endpoints must not be rejected as too thin.

        A JSON posting payload clears the length bar on its own; this guards the
        interaction with the binary check, since a payload starting with '{'
        is text but a PDF never is.
        """
        payload = '{"title": "Backend Engineer", "description": "' + "x" * 300 + '"}'
        assert payload.startswith("{")
        assert classify_text(payload) == (True, REACHABLE)

    def test_html_with_visible_text_only_in_script_is_not_reachable(self):
        html = "<html><body><script>var job = 'lots of text here that should not count';</script></body></html>"
        assert not classify_text(html)[0]


class TestTransportErrorIsNotTooThin:
    """Regression: a failed fetch must not be reported as a thin page.

    measure() originally called fetch_detail(), which catches transport
    exceptions and returns None. Both "the network failed" and "the page had no
    posting" arrived as None, so two real transport failures (builtin.com,
    ats.rippling.com) were recorded as `too_thin` -- claiming we received a page
    and found it empty, when we received nothing at all.
    """

    class _Boom:
        def fetch(self, url):
            raise RuntimeError("connection reset")

    class _EmptyPage:
        def fetch(self, url):
            return "<html><body><main><p>Short.</p></main></body></html>"

    def test_raising_client_yields_transport_error(self):
        outcome = score_reach.measure("https://example.invalid/job", self._Boom())
        assert outcome.obstacle == TRANSPORT_ERROR
        assert not outcome.reachable
        assert "RuntimeError" in (outcome.error or "")

    def test_thin_page_is_still_too_thin(self):
        outcome = score_reach.measure("https://example.invalid/job", self._EmptyPage())
        assert outcome.obstacle == TOO_THIN
        assert not outcome.reachable


class TestAggregateDenominator:
    """Regression: the rate must be computed from LABELS, not predictions.

    An earlier _aggregate() filtered on the predicted obstacle, so the live run
    reported "reach rate 0.706 (12/17)" while counting 9 unlabelled cases and 3
    not_a_posting listing pages in the denominator. Two of those "reachable" rows
    were 483KB and 131KB of careers-page theme JSON -- not postings -- which
    inflated the headline number.
    """

    def _report_with(self, rows):
        report = Report.new("posting reach rate")
        for key, label, predicted_reachable, detail_extra in rows:
            # obstacle is deliberately independent of the label: a
            # not_a_posting page can still be predicted "reachable", which is
            # exactly the conflation _aggregate must not fall for.
            detail = {
                "obstacle": "reachable",
                "reachable": predicted_reachable,
                "tier": "simple",
                **detail_extra,
            }
            scored = label not in UNSCORED_OBSTACLES and label is not None
            report.add(
                CaseResult(
                    key=key,
                    predicted=predicted_reachable,
                    label=(label == "reachable"),
                    scored=scored,
                    unscored_reason="" if scored else ("unlabelled" if label is None else ""),
                    detail={**detail, "labelled_obstacle": label},
                )
            )
        return report

    def test_unlabelled_cases_are_excluded_from_the_rate(self):
        report = self._report_with(
            [
                ("labelled_ok", "reachable", True, {}),
                ("unlabelled", None, True, {}),
            ]
        )
        agg = run_benchmark._aggregate(report, RunContext(benchmark="reach_rate"))
        assert agg["scored_cases"] == 1, agg
        assert agg["reach_rate"] == 1.0, agg
        assert agg["unscored"]["unlabelled"] == ["unlabelled"]

    def test_not_a_posting_listing_page_never_counts_as_reachable(self):
        """A 483KB careers-page JSON blob must not raise the reach rate."""
        report = self._report_with(
            [
                ("real_posting", "reachable", True, {}),
                ("listing_page", "not_a_posting", True, {"obstacle": "reachable"}),
            ]
        )
        agg = run_benchmark._aggregate(report, RunContext(benchmark="reach_rate"))
        assert agg["scored_cases"] == 1
        assert agg["reach_rate"] == 1.0
        assert agg["unscored"]["not_a_posting"] == ["listing_page"]

    def test_not_a_posting_bucket_is_reachable_from_the_detail(self):
        """Regression: the not_a_posting run branch used to drop labelled_obstacle.

        run_cases() passed detail=outcome.as_dict() for those rows, so
        _aggregate saw no label and filed both not_a_posting cases under
        "other" instead of naming them. The rate was right; the audit trail
        was not.
        """
        report = Report.new("posting reach rate")
        report.add(
            CaseResult(
                key="faq",
                predicted=True,
                label=False,
                scored=False,
                unscored_reason="not_a_posting: transport cannot judge this",
                detail={
                    "obstacle": "reachable",
                    "reachable": True,
                    "tier": "simple",
                    "labelled_obstacle": "not_a_posting",
                },
            )
        )
        agg = run_benchmark._aggregate(report, RunContext(benchmark="reach_rate"))
        assert agg["unscored"]["not_a_posting"] == ["faq"], agg["unscored"]
        assert agg["unscored"]["other"] == [], agg["unscored"]
        assert agg["scored_cases"] == 0

    def test_empty_denominator_reports_none_not_zero(self):
        report = self._report_with([("unlabelled", None, True, {})])
        agg = run_benchmark._aggregate(report, RunContext(benchmark="reach_rate"))
        assert agg["scored_cases"] == 0
        assert agg["reach_rate"] is None, "0/0 must not report 0.0 -- that reads as total failure"


class TestAgreementMode:
    """§4.1d item 5: agreement between the production path and the standalone pair."""

    def _outcome(self, url, obstacle, reachable):
        return score_reach.Outcome(url=url, reachable=reachable, obstacle=obstacle, tier="simple")

    def test_agreement_runs_both_instruments(self, monkeypatch):
        url = "https://a.example/job"

        class Client:
            def fetch(self, u):
                return "<html><body><main><p>" + "x" * 400 + "</p></main></body></html>"

        # Production side: fetch_detail succeeds (default behaviour on this html).
        rows = run_benchmark.run_agreement([url], client=Client())
        assert rows == [
            {"url": url, "production": "reachable", "standalone": "reachable", "agree": True}
        ]

    def test_agreement_detects_divergence(self):
        # production path (fetch_detail) sees a transport failure; the standalone
        # pair catches it and scores the page — exactly the 4.1d D1 divergence.
        raise_first = [True]

        class Client:
            def fetch(self, url):
                if raise_first[0]:
                    raise_first[0] = False
                    raise RuntimeError("connection reset")
                return "<html><body><main><p>" + "x" * 400 + "</p></main></body></html>"

        rows = run_benchmark.run_agreement(["https://b.example/job"], client=Client())
        assert rows[0]["agree"] is False
        assert rows[0]["production"] == "transport_error"
        assert rows[0]["standalone"] == "reachable"


class TestEntriesMode:
    """§4.1d item 2: entry set re-derived from the DB's verified companies."""

    def test_entries_from_db(self, monkeypatch):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT,"
            " careers_url TEXT, verification_status TEXT)"
        )
        conn.execute(
            "INSERT INTO companies (name, careers_url, verification_status) VALUES"
            " ('A', 'https://a.example/careers', 'verified'),"
            " ('B', 'https://b.example/careers', 'unverified')"
        )
        monkeypatch.setattr(run_benchmark.sqlite3, "connect", lambda *a, **kw: conn)
        entries = run_benchmark.derive_entries(":memory:")
        assert entries == ["https://a.example/careers"]


class TestReport:
    """Report tracks per-case correctness; the rate itself is the scorer's job."""

    def test_correctness_recorded_per_case(self):
        report = Report.new("posting reach rate")
        report.add(harness.CaseResult(key="a", predicted=True, label=True, scored=True))
        report.add(harness.CaseResult(key="b", predicted=False, label=True, scored=True))
        assert report.per_case["a"]["correct"] is True
        assert report.per_case["b"]["correct"] is False

    def test_unscored_case_records_no_correctness(self):
        """An unlabelled case must not be silently counted as correct."""
        report = Report.new("x")
        report.add(harness.CaseResult(key="c", predicted=None, label=None, scored=False, unscored_reason="unlabelled"))
        assert "correct" not in report.per_case["c"]
        assert report.per_case["c"]["scored"] is False

    def test_obstacle_label_is_kept_for_audit(self):
        """A binary/boolean match can hide a wrong obstacle; keep both visible."""
        report = Report.new("x")
        report.add(harness.CaseResult(key="a", predicted="too_thin", label="too_thin", scored=True))
        assert report.per_case["a"]["predicted"] == "too_thin"
        assert report.per_case["a"]["label"] == "too_thin"

    def test_run_context_carries_benchmark_name(self):
        assert RunContext(benchmark="x").benchmark == "x"


class TestDbAuditDetection:
    def test_classifier_flags_raw_pdf_bytes(self):
        """Regression test for a real DB row: a privacy-policy PDF stored as a
        job description. Detection only — the row is not mutated."""
        stored = "%PDF-1.7\r\n%����\r\n1 0 obj\r\n<< /Length 1024 >>\r\n"
        reachable, obstacle = classify_text(stored)
        assert obstacle == BINARY_DOCUMENT

    def test_classifier_flags_login_wall_row(self):
        """Regression test for the real jobs.siemens.com rows (763 chars)."""
        stored = "Already registered? Login Email Password Log in with a code Forgot your password?"
        assert classify_text(stored)[1] == LOGIN_WALL
