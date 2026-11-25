"""Migration 0015: companies.run_id, the run whose listing produced a candidate.

Properties pinned here: exactly one head (0015 chains onto 0014), the upgrade
adds the column and its index to a database that already has a ``companies``
table and is idempotent, the downgrade removes only them, and a fresh schema
declares the SET NULL foreign key to ``job_runs``.
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
    / "packages/core/alembic/versions/0015_companies_run_id.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0015", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0015_chains_onto_0014_as_the_single_head():
    module = _load_migration()
    assert module.revision == "0015_companies_run_id"
    assert module.down_revision == "0014_recipe_flags"


def test_upgrade_adds_column_and_index_and_downgrade_removes_only_them(tmp_path):
    """Against a pre-existing ``companies`` table, as on the owner's database."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    module = _load_migration()

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        ctx = MigrationContext.configure(conn)

        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # a second run must be a no-op

        inspector = inspect(conn)
        assert "run_id" in {c["name"] for c in inspector.get_columns("companies")}
        assert "ix_companies_run_id" in {i["name"] for i in inspector.get_indexes("companies")}

        with Operations.context(ctx):
            module.downgrade()

        inspector = inspect(conn)
        assert "run_id" not in {c["name"] for c in inspector.get_columns("companies")}
        assert "ix_companies_run_id" not in {
            i["name"] for i in inspector.get_indexes("companies")
        }
        assert "name" in {c["name"] for c in inspector.get_columns("companies")}


def test_a_fresh_schema_declares_the_set_null_foreign_key(db_url):
    init_db(db_url)
    inspector = inspect(get_engine())
    columns = {c["name"]: c for c in inspector.get_columns("companies")}
    assert columns["run_id"]["nullable"] is True

    keys = [
        fk
        for fk in inspector.get_foreign_keys("companies")
        if fk["constrained_columns"] == ["run_id"]
    ]
    assert keys, "companies.run_id must declare its foreign key on a fresh schema"
    assert keys[0]["referred_table"] == "job_runs"
    assert keys[0]["options"].get("ondelete", "").upper() == "SET NULL"
