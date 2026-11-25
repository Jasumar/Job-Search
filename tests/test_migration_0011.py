"""Migration 0011: companies.recipe_state and companies.recipe_trials."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "packages/core/alembic/versions/0011_company_recipe_state.py"
)
_NOW = datetime(2026, 10, 18)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0011", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_company_defaults_to_ok_with_zero_trials(session):
    company = Company(name="Acme", careers_url=None)
    session.add(company)
    session.commit()
    session.refresh(company)
    assert company.recipe_state == "ok"
    assert company.recipe_trials == 0


def test_upgrade_is_idempotent_and_downgrade_keeps_companies(db_url, session):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        cols = {c["name"] for c in inspect(conn).get_columns("companies")}
        assert {"recipe_state", "recipe_trials"} <= cols
        with Operations.context(ctx):
            module.downgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("companies")}
        assert "recipe_state" not in cols and "recipe_trials" not in cols
        assert "companies" in inspect(conn).get_table_names()
