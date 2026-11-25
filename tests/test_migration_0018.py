"""0018 repair migration: company_xhr_capture.replay_reason.

Property: the upgrade adds the column to a table that lacks it (the drifted
shape), is a no-op the second time, and leaves existing rows intact.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0018_capture_replay_reason.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0018", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(conn, table: str) -> set[str]:
    return {col["name"] for col in inspect(conn).get_columns(table)}


def test_upgrade_adds_the_missing_column_and_is_idempotent(db_url):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()

    with engine.begin() as conn:
        # Reproduce the drifted shape: the table exists without the column.
        conn.exec_driver_sql("ALTER TABLE company_xhr_capture DROP COLUMN replay_reason")
        assert "replay_reason" not in _columns(conn, "company_xhr_capture")

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            assert "replay_reason" in _columns(conn, "company_xhr_capture")
            module.upgrade()  # second run must be a no-op
            assert "replay_reason" in _columns(conn, "company_xhr_capture")

        with Operations.context(ctx):
            module.downgrade()
        columns = _columns(conn, "company_xhr_capture")
        assert "replay_reason" not in columns
        # Neighbouring revisions' columns survive the downgrade.
        assert {"content_type", "body_sha256", "body_truncated", "capture_batch"} <= columns


def test_upgrade_keeps_existing_rows(db_url, session):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()

    company = Company(name="NVIDIA", careers_url="https://jobs.nvidia.com")
    session.add(company)
    session.commit()

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO company_xhr_capture "
            "(company_id, captured_at, url, method, status, response_bytes,"
            " sample_titles, replay_matches, class_a_candidate) "
            "VALUES (?, '2026-10-13 00:00:00', 'https://jobs.nvidia.com/api', 'GET',"
            " 200, 96, NULL, NULL, 1)",
            (company.id,),
        )

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        rows = conn.exec_driver_sql(
            "SELECT company_id, url, replay_reason FROM company_xhr_capture"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "https://jobs.nvidia.com/api"
        assert rows[0][2] is None
