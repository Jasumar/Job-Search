"""The benchmark report must render every snapshot shape without blowing up.

It is the only thing a human reads after a 25-company run, so a KeyError in it
costs the whole run's evidence: the capture-gap companies, the replayed ones and
the absent-provenance ones (snapshots written before the derived reference) all
share one loop.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "recipe_author"))

import report  # noqa: E402

AUTHOR_DATE = "2026-01-01"


def _snapshot(key: str, status: str, reference: dict, **extra) -> dict:
    return {
        "key": key,
        "name": f"{key.title()} Inc.",
        "stratum": "employer",
        "family": 1,
        "open_positions_kind_label": "employer",
        "run_date": AUTHOR_DATE,
        "model": "test-model",
        "status": status,
        "notes": {"page_url": f"https://{key}.test/jobs"},
        "artifacts": [
            {"name": "static_html", "fetch_mode": "static", "url": "", "bytes": 1, "file": "x.html"},
            {"name": "json_1", "fetch_mode": "xhr", "url": "", "bytes": 1, "file": "x.json"},
        ],
        "reference": reference,
        "scraper": {"count": 3, "in_reference": 2, "outside_reference": 1},
        **extra,
    }


@pytest.fixture()
def rendered(monkeypatch, tmp_path) -> str:
    src = tmp_path / "snapshots" / AUTHOR_DATE
    src.mkdir(parents=True)
    # A company whose capture held no posting list at all.
    (src / "blank.json").write_text(
        json.dumps(
            _snapshot(
                "blank",
                "capture_gap",
                {"count": 0, "method": None, "confidence": None, "notes": ["json_1: 0 ids in list"]},
            )
        )
    )
    # A company written before the derived reference existed: no provenance fields.
    (src / "old.json").write_text(
        json.dumps(
            _snapshot(
                "old",
                "failed",
                {"count": 2, "sample": [{"title": "Old posting", "url": "https://old.test/j/1"}]},
                trials=[{"number": 1, "passed": False, "reason": "empty list is never success"}],
            )
        )
    )
    # A company with a stored recipe and a failing replay of it.
    (src / "played.json").write_text(
        json.dumps(
            _snapshot(
                "played",
                "verified",
                {
                    "count": 4,
                    "method": "json",
                    "confidence": "structured",
                    "carried_by": "json_1",
                    "by_artifact": {"json_1": 4},
                    "sample": [{"title": "Backend Engineer", "url": "https://played.test/j/1"}],
                },
                recipe_body={"kind": "api", "url_template": "https://played.test/api?page={page}"},
            )
        )
    )
    # A company whose body already has a gate answer and nothing fired since.
    (src / "quiet.json").write_text(
        json.dumps(
            _snapshot(
                "quiet",
                "verified",
                {"count": 2, "method": "json", "confidence": "structured", "carried_by": "json_1"},
                recipe_body={"kind": "api", "url_template": "https://quiet.test/api?page={page}"},
            )
        )
    )
    today = tmp_path / "snapshots" / date.today().isoformat()
    today.mkdir(parents=True)
    (today / "quiet.replay.json").write_text(
        json.dumps({"key": "quiet", "status": "ok", "count": 2, "gate_event": False})
    )
    (today / "played.replay.json").write_text(
        json.dumps(
            {
                "key": "played",
                "replay_run_date": date.today().isoformat(),
                "status": "ok",
                "count": 0,
                # A gate failure with no reason string: the report must not KeyError.
                "verify_on_fresh_capture": {
                    "ok": False,
                    "count": 0,
                    "off_pattern": 1,
                    "missing_titles": [],
                    "outside_reference": 0,
                },
                "vs_fresh_reference": {"reference": 4, "matched": 0, "unmatched": 0, "missing": 4},
                "reference_drift": {"run1": 4, "today": 4, "gone_count": 0, "new_count": 0},
                # R3 evidence: routers, a fired signal, a flagged-but-continuing gate.
                "body_sha256": "abc123",
                "dom_agreement": {"replay": 4, "rendered": 6, "matched": 3, "missing": 1},
                "statistics": {
                    "count": 4,
                    "fill": {"location": 0.5, "posted_date": None, "description": 1.0},
                    "duplicate_urls": 1,
                    "duplicate_titles": 0,
                    "page_counts": [2, 0],
                    "page_advance": False,
                    "detail": [3, 4],
                },
                "signals": [
                    {"name": "duplicate_urls", "value": 1, "points_at": "url extraction"},
                ],
                "gate": {
                    "verdict": "none",
                    "model_id": "test-model",
                    "items": [{"url": "https://played.test/nav", "reason": "a navigation link"}],
                    "stopped": False,
                },
            }
        )
    )
    monkeypatch.setattr(report, "SNAP_ROOT", tmp_path / "snapshots")
    monkeypatch.setattr(report, "BENCH", tmp_path)
    monkeypatch.setattr(sys, "argv", ["report.py", AUTHOR_DATE])
    assert report.main() == 0
    return (tmp_path / f"report-{AUTHOR_DATE}.md").read_text()


def test_capture_gap_is_reported_as_a_capture_problem(rendered: str) -> None:
    assert "CAPTURE GAP: no posting list in the capture" in rendered
    assert "nothing was graded" in rendered


def test_a_failing_replay_without_a_reason_still_renders(rendered: str) -> None:
    assert "Verify on fresh fetch: FAIL: empty replay" in rendered
    assert "off-pattern 1" in rendered


def test_missing_provenance_reads_as_a_dash(rendered: str) -> None:
    assert "method -/-" in rendered
    assert "by artifact -" in rendered
    assert "None/None" not in rendered


def test_r3_evidence_renders(rendered: str) -> None:
    assert "First page vs rendered DOM: 3/4 replayed in DOM, 1 missing, 6 DOM links" in rendered
    assert "Statistics: dups 1 url / 0 title, pages [2, 0] advance=False, detail 3/4" in rendered
    assert "Signal duplicate_urls=1: url extraction" in rendered
    # A "none" without an independent agreement flags and keeps going.
    assert "Gate (test-model): none -> flagged, continuing (no agreement yet)" in rendered
    assert "https://played.test/nav: a navigation link" in rendered
    assert "STOP" not in rendered


def test_a_skipped_gate_reads_as_cadence(rendered: str) -> None:
    assert "Gate: skipped (existing answer, no event)" in rendered


def test_all_companies_reach_the_report(rendered: str) -> None:
    for key in ("blank", "old", "played", "quiet"):
        assert f"## {key} " in rendered
