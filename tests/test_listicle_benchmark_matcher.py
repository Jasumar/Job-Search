"""Offline tests for the listicle benchmark matcher (no LLM, no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "listicle"
sys.path.insert(0, str(BENCH_DIR))

# Other benchmark entries also ship a module called `run_benchmark`
# (benchmarks/reach/). Importing it by bare name makes this test depend on
# which module pytest happens to collect first, so load it by path under a
# unique name instead.
_spec = importlib.util.spec_from_file_location(
    "listicle_run_benchmark", BENCH_DIR / "run_benchmark.py"
)
assert _spec and _spec.loader
run_benchmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_benchmark
_spec.loader.exec_module(run_benchmark)

legal_form = run_benchmark.legal_form
match_names = run_benchmark.match_names
normalize = run_benchmark.normalize

SITES_PATH = BENCH_DIR / "data" / "sites.json"


def load_sites() -> dict[str, str]:
    return json.loads(SITES_PATH.read_text(encoding="utf-8"))


class TestNormalize:
    def test_casefold_and_punctuation(self):
        assert normalize("  Nokia OY! ") == "nokia oy"

    def test_parenthetical_dropped(self):
        assert normalize("Alphabet (Google)") == "alphabet"

    def test_ampersand(self):
        assert normalize("H&M") == "h and m"

    def test_never_strips_legal_form(self):
        # I1 invariant: "Foo Oy" must NOT normalize to "foo"
        assert normalize("Foo Oy") == "foo oy"
        assert normalize("Foo GmbH") == "foo gmbh"


class TestLegalForm:
    def test_detects_suffix(self):
        assert legal_form("foo oy") == "oy"
        assert legal_form("kioxia holdings corporation") == "corporation"

    def test_none_without_suffix(self):
        assert legal_form("nvidia") is None
        assert legal_form("sony group") is None

    def test_differing_forms_differ(self):
        assert legal_form(normalize("Foo Oy")) != legal_form(normalize("Foo GmbH"))


class TestMatchNames:
    def test_exact_match(self):
        res = match_names(["Nokia"], ["NOKIA"])
        assert len(res["matched"]) == 1
        assert res["matched"][0]["similarity"] >= 0.85
        assert res["misses"] == []
        assert res["noise"] == []

    def test_fuzzy_match_within_threshold(self):
        res = match_names(["Kioxia Holdings Corporation"], ["Kioxia Holdings Corp."])
        # 'corp' vs 'corporation' are DIFFERENT legal forms -> must NOT match
        assert res["matched"] == []
        assert res["misses"] == ["Kioxia Holdings Corporation"]

    def test_fuzzy_match_same_form(self):
        res = match_names(["Kioxia Holdings Corporation"], ["KIOXIA Holdings  Corporation"])
        assert len(res["matched"]) == 1

    def test_collision_guard_blocks_cross_suffix_match(self):
        # I1: two companies differing only by legal form must never match,
        # no matter how similar the strings are.
        res = match_names(["Foo Oy"], ["Foo Ab"])
        assert res["matched"] == []
        assert res["misses"] == ["Foo Oy"]
        assert res["noise"] == ["Foo Ab"]

    def test_one_to_one_assignment(self):
        # greedy matcher must not spend both gt names on one extracted name
        res = match_names(
            ["Alpha Inc", "Alpha Industries Inc"],
            ["Alpha Industries Inc"],
        )
        assert len(res["matched"]) == 1
        assert len(res["misses"]) == 1

    def test_noise_and_misses(self):
        res = match_names(["A", "B"], ["A", "C", "D"])
        assert len(res["matched"]) == 1
        assert res["misses"] == ["B"]
        assert set(res["noise"]) == {"C", "D"}

    def test_empty_extracted(self):
        res = match_names(["A"], [])
        assert res["matched"] == []
        assert res["misses"] == ["A"]
        assert res["noise"] == []


class TestNearMisses:
    """I5 (original spellings) and I3 (one-to-one) for the near-miss list.

    The near-miss list is the threshold-eyeball instrument: a reviewer decides
    from it whether the 0.85 bar is too strict. Both invariants exist so that
    reading is possible — normalized spellings are not actionable, and a
    repeated candidate makes the score look like it belongs to each GT row.
    """

    def test_candidate_keeps_original_extracted_spelling(self):
        # I5: normalization is for comparison only; the report must show the
        # string a reviewer would actually see on the page.
        # 0.80 — below the 0.85 bar, so this pair only ever appears as a review
        # candidate. Normalization would render it "alpha industr solutions oy".
        res = match_names(["Alpha Industries Oy"], ["Alpha Industr Solutions Oy"])
        assert res["matched"] == []
        near = res["near_misses"]
        assert len(near) == 1
        assert near[0]["best_candidate"] == "Alpha Industr Solutions Oy"
        assert near[0]["ground_truth"] == "Alpha Industries Oy"

    def test_candidate_is_not_reused_across_ground_truth_rows(self):
        # I3: one extraction may be offered against at most one GT name.
        # Both GT rows miss (0.80 and 0.78 against the single extraction), so
        # both would have claimed it before the exclusivity fix. One extraction
        # may back at most one review row.
        # 0.667 / 0.683 / 0.700 against the one extraction — all three rows miss,
        # so all three would have claimed it before the exclusivity fix.
        res = match_names(
            [
                "Alpha Industries Oy",
                "Alpha Industry Solutions Oy",
                "Alpha Industy Solutions Oy",
            ],
            ["Alpha Solns Oy"],
        )
        assert len(res["misses"]) == 3
        near = res["near_misses"]
        assert len(near) == 1
        assert near[0]["best_candidate"] == "Alpha Solns Oy"
        assert len({nm["best_candidate"] for nm in near}) == len(near)

    def test_near_miss_never_counts_as_a_match(self):
        # I4: near misses are human-review output, never recall.
        res = match_names(["Alpha Industries Oy"], ["Alpha Industrial Solutions Oy"])
        assert res["matched"] == []
        assert res["misses"] == ["Alpha Industries Oy"]
        assert res["near_misses"]  # reported for review...
        assert res["near_misses"][0]["ground_truth"] not in {m["ground_truth"] for m in res["matched"]}

    def test_matched_extraction_is_not_re_offered_as_a_near_miss(self):
        # 1.00 for the exact pair, 0.700 for the misspelled one. The matched
        # GT row is closed, so its spent extraction can never reappear as a
        # review candidate, and no new GT row exists to claim the other one.
        res = match_names(
            ["Alpha Industries Oy"],
            ["Alpha Industries Oy", "Alpha Solns Oy"],
        )
        assert len(res["matched"]) == 1
        assert res["near_misses"] == []
        assert res["noise"] == ["Alpha Solns Oy"]


    def test_no_near_miss_across_a_legal_form_collision(self):
        # The collision guard applies to the review list too: a candidate with
        # a different legal form is a different company, not a near miss.
        res = match_names(["Foo Oy"], ["Foo Ab"])
        assert res["near_misses"] == []


class TestBenchDataConsistency:
    """The sample and the mining cap must not silently cap recall.

    The 2026-09-23 run reported 0% for a site the miner could in fact read
    because the extracted list was truncated at the mining cap before matching;
    these invariants keep the data and the cap honest.
    """

    def test_every_site_has_ground_truth_and_a_sample_plan(self):
        gt = run_benchmark.load_ground_truth()
        assert set(gt) == set(load_sites())

    def test_ground_truth_names_are_unique(self):
        for key, names in run_benchmark.load_ground_truth().items():
            assert len(set(names)) == len(names), f"duplicate ground-truth name in {key}"

    def test_sample_plan_never_exceeds_ground_truth(self):
        for key, names in run_benchmark.load_ground_truth().items():
            take = run_benchmark.SAMPLE_PLAN.get(key, len(names))
            assert 0 < take <= len(names), key

    def test_deepest_sampled_index_is_reachable_under_the_mining_cap(self):
        # Head 15 + spread reaches index 85 of a 100-name list; with the old
        # cap of 60 that made 100% recall unreachable by construction.
        for key, names in run_benchmark.load_ground_truth().items():
            take = run_benchmark.SAMPLE_PLAN.get(key, len(names))
            sample = run_benchmark.build_sample(names, take)
            deepest = max(names.index(name) for name in sample)
            assert deepest < run_benchmark.BENCH_MAX_COMPANIES, (
                f"{key}: sample needs ground-truth index {deepest}, "
                f"cap is {run_benchmark.BENCH_MAX_COMPANIES}"
            )
