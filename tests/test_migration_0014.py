"""Migration 0014: the recipe_flags table.

Properties pinned here: exactly one head (0014 chains onto 0013), upgrade is
idempotent, downgrade removes only this table, and the verdict check constraint
refuses a value the gate could never produce.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "packages/core/alembic/versions/0014_recipe_flags.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0014", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0014_chains_onto_0013_as_the_single_head():
    module = _load_migration()
    assert module.revision == "0014_recipe_flags"
    assert module.down_revision == "0013_recipe_evidence"


def test_upgrade_is_idempotent_and_downgrade_drops_only_this_table(db_url, session):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        assert "recipe_flags" in inspect(conn).get_table_names()
        with Operations.context(ctx):
            module.downgrade()
        assert "recipe_flags" not in inspect(conn).get_table_names()
        assert "company_recipes" in inspect(conn).get_table_names()


def test_verdict_constraint_accepts_only_the_two_gate_answers(session):
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO recipe_flags (body_sha256, verdict, analysis) "
                "VALUES (:sha, 'maybe', '{}')"
            ),
            {"sha": "a" * 64},
        )
        session.flush()


def test_a_flag_survives_without_a_recipe_row(session):
    session.execute(
        text(
            "INSERT INTO recipe_flags (body_sha256, verdict, analysis, stopped) "
            "VALUES (:sha, 'none', '{}', 1)"
        ),
        {"sha": "b" * 64},
    )
    session.commit()
    stopped = session.execute(text("SELECT stopped FROM recipe_flags")).scalar_one()
    assert stopped == 1
