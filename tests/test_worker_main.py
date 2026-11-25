from __future__ import annotations

import pytest
from jobsearch_core.db.session import get_session, init_db
from jobsearch_worker import consumer
from jobsearch_worker.main import _recover_stale_running_runs


@pytest.fixture
def db(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    init_db(database_url)
    yield


def test_recover_stale_running_runs_fails_stuck_rows(db):
    from jobsearch_core.db.models import JobRun

    session = get_session()
    try:
        stuck = consumer.enqueue_run(session, "discover", {"dry_run": True})
        stuck.status = "running"
        session.commit()
        stuck_id = stuck.id
        queued = consumer.enqueue_run(session, "collect", {"dry_run": True})
        queued_id = queued.id
    finally:
        session.close()

    recovered = _recover_stale_running_runs()
    assert recovered == 1

    session = get_session()
    try:
        stuck_after = session.get(JobRun, stuck_id)
        queued_after = session.get(JobRun, queued_id)
        assert stuck_after.status == "failed"
        assert stuck_after.error is not None
        assert stuck_after.finished_at is not None
        assert queued_after.status == "queued"
    finally:
        session.close()


def test_recovered_run_no_longer_blocks_enqueue(db):
    """A stale ``running`` row must not permanently trip the one-active-run rule."""
    session = get_session()
    try:
        stuck = consumer.enqueue_run(session, "discover", {"dry_run": True})
        stuck.status = "running"
        session.commit()
    finally:
        session.close()

    _recover_stale_running_runs()

    session = get_session()
    try:
        fresh = consumer.enqueue_run(session, "discover", {"dry_run": True})
        assert fresh.status == "queued"
    finally:
        session.close()


def test_recover_is_a_noop_without_stale_rows(db):
    assert _recover_stale_running_runs() == 0
