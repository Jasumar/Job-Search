"""RF9 (reads load what they use) and RF10 (SQLite WAL + busy timeout)."""

from __future__ import annotations

from unittest.mock import patch

from jobsearch_core.db.models import Application, Job
from jobsearch_core.db.session import get_engine, get_session
from jobsearch_core.run import list_jobs
from sqlalchemy import Engine, event, inspect


def _add(session, n: int, **kwargs) -> int:
    job = Job(
        url=f"https://example.com/j/{n}",
        raw_title=f"Title {n}",
        raw_company="Co",
        description=kwargs.pop("description", "A long description"),
        **kwargs,
    )
    session.add(job)
    session.commit()
    return job.id


def test_every_connection_is_wal_with_a_busy_timeout():
    engine = get_engine()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 5000


def test_list_jobs_applies_limit_and_tailored_in_sql(db_url):
    session = get_session()
    ids = [_add(session, n) for n in range(5)]
    session.add(Application(job_id=ids[3], base_variant="backend", status="draft"))
    session.commit()
    session.close()

    statements: list[str] = []

    def record(conn, cur, stmt, *args):
        statements.append(stmt)

    # list_jobs rebinds the engine through init_db, so listen on the class.
    event.listen(Engine, "before_cursor_execute", record)
    try:
        with patch("jobsearch_core.run.Settings") as mock_settings:
            mock_settings.return_value.database_url = db_url
            limited = list_jobs(limit=2)
            tailored = list_jobs(tailored=True)
            untailored = list_jobs(tailored=False, limit=10)
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert [j.id for j in limited] == ids[:2]
    assert [j.id for j in tailored] == [ids[3]]
    assert [j.id for j in untailored] == [i for i in ids if i != ids[3]]
    assert any("LIMIT" in s for s in statements)
    # The large text columns are not selected by the list query.
    jobs_selects = [s for s in statements if s.startswith("SELECT") and "FROM jobs" in s]
    assert jobs_selects
    assert all("jobs.description" not in s for s in jobs_selects)
    assert all("jobs.job_profile" not in s for s in jobs_selects)


def test_list_jobs_result_has_no_description_loaded(db_url):
    session = get_session()
    _add(session, 1)
    session.close()
    with patch("jobsearch_core.run.Settings") as mock_settings:
        mock_settings.return_value.database_url = db_url
        (job,) = list_jobs()
    assert "description" not in inspect(job).dict
    assert "job_profile" not in inspect(job).dict


def test_match_loads_descriptions_in_one_batch_not_per_row(db_url):
    """Eligibility reads only 'has a description'; candidate text is one query."""
    from jobsearch_core.config import Settings
    from jobsearch_core.run import match_jobs

    session = get_session()
    for n in range(6):
        _add(session, n)
    _add(session, 99, description="")  # no description: never a candidate
    session.close()

    statements: list[str] = []

    def record(conn, cur, stmt, *args):
        statements.append(stmt)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        with patch("jobsearch_core.run.check_link_liveness"):
            summary = match_jobs(settings=Settings(), evidence=None)
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert summary.jobs_found == 7
    # No profile on any job: all six candidates are pending, none scored.
    assert summary.jobs_pending == 6
    per_row = [
        s for s in statements
        if "FROM jobs" in s and "WHERE jobs.id = ?" in s
    ]
    assert per_row == [], "deferred columns were loaded one row at a time"
