"""Migration 0017: run_sources.author_trace, the M1 authoring trials' evidence.

Properties pinned here: exactly one head (0017 chains onto 0016), the upgrade
adds the column to a database that already has a ``run_sources`` table and is
idempotent, the downgrade removes only it, and a fresh schema declares it
nullable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0017_run_source_author_trace.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0017", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0017_chains_onto_0016_as_the_single_head():
    module = _load_migration()
    assert module.revision == "0017_run_source_author_trace"
    assert module.down_revision == "0016_run_source_entry_trace"


def test_upgrade_adds_column_and_downgrade_removes_only_it(tmp_path):
    """Against a pre-existing ``run_sources`` table, as on the owner's database."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    module = _load_migration()

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE run_sources (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL)"
        )
        ctx = MigrationContext.configure(conn)

        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # a second run must be a no-op

        inspector = inspect(conn)
        assert "author_trace" in {c["name"] for c in inspector.get_columns("run_sources")}

        with Operations.context(ctx):
            module.downgrade()

        inspector = inspect(conn)
        assert "author_trace" not in {c["name"] for c in inspector.get_columns("run_sources")}
        assert "run_id" in {c["name"] for c in inspector.get_columns("run_sources")}


def test_a_fresh_schema_declares_the_column_nullable(db_url):
    init_db(db_url)
    inspector = inspect(get_engine())
    columns = {c["name"]: c for c in inspector.get_columns("run_sources")}
    assert columns["author_trace"]["nullable"] is True
