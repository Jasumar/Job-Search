"""0019 migration: job_exclusions.

Property: upgrade creates the table once (idempotent), a duplicate fingerprint is
rejected by the unique constraint, and downgrade removes the table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0019_job_exclusions.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0019", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_idempotent_and_downgrade_drops_the_table(db_url):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE job_exclusions")
        assert "job_exclusions" not in inspect(conn).get_table_names()

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        assert "job_exclusions" in inspect(conn).get_table_names()

        with Operations.context(ctx):
            module.downgrade()
        assert "job_exclusions" not in inspect(conn).get_table_names()


def test_duplicate_fingerprint_is_rejected(db_url):
    init_db(db_url)
    engine = get_engine()
    insert = (
        "INSERT INTO job_exclusions (dedup_hash, url) "
        "VALUES ('abc123', 'https://example.com/jobs/1')"
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(insert)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.exec_driver_sql(insert)
