"""Phase 4.3 migration: company_xhr_capture evidence table.

Property: the table is evidence only. Creating it is idempotent, rows reference
a real company, and downgrade removes the table without touching companies.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company, CompanyXhrCapture
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0008_company_xhr_capture.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0008", _MIGRATION)
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
        assert "company_xhr_capture" in inspect(conn).get_table_names()
        columns = {col["name"] for col in inspect(conn).get_columns("company_xhr_capture")}
        assert "replay_reason" in columns
        with Operations.context(ctx):
            module.downgrade()
        assert "company_xhr_capture" not in inspect(conn).get_table_names()
        assert "companies" in inspect(conn).get_table_names()


def test_capture_row_references_company(db_url, session):
    init_db(db_url)
    company = Company(name="NVIDIA", careers_url="https://jobs.nvidia.com/careers")
    session.add(company)
    session.commit()
    from datetime import datetime

    session.add(
        CompanyXhrCapture(
            company_id=company.id,
            captured_at=datetime(2026, 10, 13),
            url="https://jobs.nvidia.com/api/pcsx/search?start=0",
            method="GET",
            status=200,
            response_bytes=9600,
            sample_titles='["DevOps Engineer, DOCA"]',
            replay_matches=None,
            class_a_candidate=True,
        )
    )
    session.commit()
    row = session.query(CompanyXhrCapture).one()
    assert row.company_id == company.id
    assert row.replay_matches is None
    # Evidence never sets the company's class.
    assert company.site_class is None
