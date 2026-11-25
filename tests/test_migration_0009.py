"""Phase F migration: company_recipes table (plan-scraping §Recipe authoring).

Properties: upgrade is idempotent; downgrade drops only this table; both recipe
scopes persist; the CHECK constraints reject rows that name no owner or an
unknown kind/status.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company, CompanyRecipe
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0009_company_recipes.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0009", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_idempotent_and_downgrade_keeps_companies(db_url, session):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        assert "company_recipes" in inspect(conn).get_table_names()
        columns = {c["name"] for c in inspect(conn).get_columns("company_recipes")}
        assert {"scope", "target_url", "body", "verification"} <= columns
        with Operations.context(ctx):
            module.downgrade()
        assert "company_recipes" not in inspect(conn).get_table_names()
        assert "companies" in inspect(conn).get_table_names()


def test_both_scopes_persist_and_owner_is_enforced(db_url, session):
    init_db(db_url)
    company = Company(name="Nordea", careers_url="https://www.nordea.com/careers")
    session.add(company)
    session.commit()
    now = datetime(2026, 10, 17)
    session.add(
        CompanyRecipe(
            scope="company_careers",
            company_id=company.id,
            kind="dom",
            body="{}",
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        CompanyRecipe(
            scope="m1_url",
            target_url="https://www.nordea.com/careers?fPage=2",
            kind="api",
            body="{}",
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    rows = session.query(CompanyRecipe).order_by(CompanyRecipe.id).all()
    assert [r.status for r in rows] == ["candidate", "candidate"]
    assert rows[0].company_id == company.id and rows[1].target_url.endswith("fPage=2")


@pytest.mark.parametrize(
    "fields",
    [
        {"scope": "company_careers", "kind": "dom"},  # no company_id
        {"scope": "m1_url", "kind": "api"},  # no target_url
        {"scope": "other", "kind": "api", "company_id": 1},  # unknown scope
        {"scope": "m1_url", "kind": "html", "target_url": "https://x"},  # unknown kind
    ],
)
def test_invalid_recipe_rows_are_rejected(db_url, session, fields):
    init_db(db_url)
    company = Company(name="Acme")
    session.add(company)
    session.commit()
    fields = dict(fields)
    if fields.get("company_id") == 1:
        fields["company_id"] = company.id
    now = datetime(2026, 10, 17)
    session.add(CompanyRecipe(body="{}", created_at=now, updated_at=now, **fields))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
