"""Verified-URL evidence and drift comparison for run sources (R1).

Property: run N is compared to run N-1 by URL set, never by count, and a run
without URL evidence is never used as a baseline.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from jobsearch_core.db import source_evidence as se
from jobsearch_core.db.models import JobRun, RunSource, Source

NOW = datetime(2026, 10, 28, 9, 0, 0)


def _run(session) -> JobRun:
    run = JobRun(run_type="name_collect")
    session.add(run)
    session.flush()
    return run


def _source(session, url: str = "https://example.fi/jobs") -> Source:
    source = Source(url=url, created_at=NOW, updated_at=NOW)
    session.add(source)
    session.flush()
    return source


def _run_source(session, run: JobRun, source: Source | None = None, *, company_id=None) -> RunSource:
    row = RunSource(run_id=run.id, source_id=source.id if source else None, company_id=company_id)
    session.add(row)
    session.flush()
    return row


def test_record_stores_sorted_unique_set_and_source_count(session):
    source = _source(session)
    row = _run_source(session, _run(session), source)

    written = se.record_verified_urls(
        session, row.id, ["https://a/2", "https://a/1", "https://a/2"]
    )

    assert written == 2
    assert json.loads(row.verified_urls) == ["https://a/1", "https://a/2"]
    assert source.last_verified_count == 2
    assert source.updated_at != NOW


def test_equal_sets_encode_identically_regardless_of_order(session):
    first = _run_source(session, _run(session), _source(session, "https://x/1"))
    second = _run_source(session, _run(session), _source(session, "https://x/2"))

    se.record_verified_urls(session, first.id, ["https://j/b", "https://j/a"])
    se.record_verified_urls(session, second.id, ["https://j/a", "https://j/b"])

    assert first.verified_urls == second.verified_urls


def test_careers_search_row_writes_no_source_count(session):
    row = _run_source(session, _run(session), company_id=7)
    assert se.record_verified_urls(session, row.id, ["https://j/a"]) == 1
    assert json.loads(row.verified_urls) == ["https://j/a"]


def test_missing_row_writes_nothing(session):
    assert se.record_verified_urls(session, 424242, ["https://j/a"]) == 0
    assert session.query(RunSource).count() == 0


def test_empty_url_is_rejected_not_dropped(session):
    """A dropped URL would read as a disappeared posting in the next diff."""
    row = _run_source(session, _run(session), _source(session))
    with pytest.raises(ValueError):
        se.record_verified_urls(session, row.id, ["https://j/a", ""])


def test_first_run_has_no_baseline(session):
    row = _run_source(session, _run(session), _source(session))
    se.record_verified_urls(session, row.id, ["https://j/a"])
    assert se.compare_to_previous(session, row.id) is None


def test_drift_compares_sets_not_counts(session):
    """Same size, different URL: one replaced posting is still drift."""
    source = _source(session)
    first = _run_source(session, _run(session), source)
    se.record_verified_urls(session, first.id, ["https://j/a", "https://j/b"])

    second = _run_source(session, _run(session), source)
    se.record_verified_urls(session, second.id, ["https://j/a", "https://j/c"])

    drift = se.compare_to_previous(session, second.id)
    assert drift is not None
    assert drift.previous_run_source_id == first.id
    assert drift.previous_count == drift.current_count == 2
    assert drift.added == ("https://j/c",)
    assert drift.removed == ("https://j/b",)
    assert drift.has_drift


def test_no_drift_when_sets_are_equal(session):
    source = _source(session)
    first = _run_source(session, _run(session), source)
    se.record_verified_urls(session, first.id, ["https://j/a"])
    second = _run_source(session, _run(session), source)
    se.record_verified_urls(session, second.id, ["https://j/a"])

    drift = se.compare_to_previous(session, second.id)
    assert drift is not None
    assert not drift.has_drift


def test_baseline_skips_runs_without_evidence(session):
    """A failed run that stored no set is not run N-1."""
    source = _source(session)
    first = _run_source(session, _run(session), source)
    se.record_verified_urls(session, first.id, ["https://j/a"])
    _run_source(session, _run(session), source)  # no URLs stored: not a baseline

    third = _run_source(session, _run(session), source)
    se.record_verified_urls(session, third.id, ["https://j/b"])

    drift = se.compare_to_previous(session, third.id)
    assert drift is not None
    assert drift.previous_run_source_id == first.id


def test_baseline_is_scoped_to_the_same_owner(session):
    """Another source's run is never the baseline."""
    other = _source(session, "https://other.fi/jobs")
    other_row = _run_source(session, _run(session), other)
    se.record_verified_urls(session, other_row.id, ["https://other/1"])

    source = _source(session)
    row = _run_source(session, _run(session), source)
    se.record_verified_urls(session, row.id, ["https://j/a"])

    assert se.compare_to_previous(session, row.id) is None


def test_careers_search_baseline_is_scoped_to_the_company(session):
    row = _run_source(session, _run(session), company_id=7)
    se.record_verified_urls(session, row.id, ["https://j/a"])
    other_company = _run_source(session, _run(session), company_id=8)
    se.record_verified_urls(session, other_company.id, ["https://j/z"])
    newer = _run_source(session, _run(session), company_id=7)
    se.record_verified_urls(session, newer.id, ["https://j/b"])

    drift = se.compare_to_previous(session, newer.id)
    assert drift is not None
    assert drift.previous_run_source_id == row.id


def test_zero_length_and_malformed_payloads(session):
    assert se.decode_urls(None) == frozenset()
    assert se.decode_urls("[]") == frozenset()
    with pytest.raises(ValueError):
        se.decode_urls('"not a list"')
    with pytest.raises(ValueError):
        se.decode_urls("[1, 2]")


def test_row_without_urls_has_no_drift(session):
    row = _run_source(session, _run(session), _source(session))
    assert se.compare_to_previous(session, row.id) is None
