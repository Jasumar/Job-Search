"""R1 migration: capture evidence columns and run_sources.verified_urls.

Property: upgrade is idempotent, existing rows keep working because the new
NOT NULL columns carry server defaults, and downgrade removes only what this
revision added.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company, CompanyXhrCapture, JobRun, RunSource
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0013_recipe_evidence.py"
)
_CAPTURE_COLUMNS = {"content_type", "body_sha256", "body_truncated", "capture_batch"}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0013", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(conn, table: str) -> set[str]:
    return {col["name"] for col in inspect(conn).get_columns(table)}


def test_upgrade_adds_columns_idempotently_and_downgrade_removes_them(db_url):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        assert _CAPTURE_COLUMNS <= _columns(conn, "company_xhr_capture")
        assert "verified_urls" in _columns(conn, "run_sources")
        with Operations.context(ctx):
            module.downgrade()
        assert not (_CAPTURE_COLUMNS & _columns(conn, "company_xhr_capture"))
        assert "verified_urls" not in _columns(conn, "run_sources")
        # Neighbouring revisions' columns survive.
        assert "replay_reason" in _columns(conn, "company_xhr_capture")
        assert "last_verified_count" in _columns(conn, "sources")


def test_existing_capture_row_gains_defaults(db_url, session):
    """Rows written before R1 must read back with a batch and truncation flag."""
    init_db(db_url)
    company = Company(name="NVIDIA", careers_url="https://jobs.nvidia.com/careers")
    session.add(company)
    session.commit()
    session.execute(
        CompanyXhrCapture.__table__.insert().values(
            company_id=company.id,
            captured_at=datetime(2026, 10, 13),
            url="https://jobs.nvidia.com/api/pcsx/search",
            method="GET",
            status=200,
            response_bytes=96,
            sample_titles=None,
            replay_matches=None,
            class_a_candidate=True,
        )
    )
    session.commit()
    row = session.query(CompanyXhrCapture).one()
    assert row.capture_batch == 0
    assert row.body_truncated is False
    assert row.content_type is None
    assert row.body_sha256 is None


def test_run_source_stores_a_url_set(db_url, session):
    init_db(db_url)
    run = JobRun(run_type="name_collect")
    session.add(run)
    session.flush()
    session.add(
        RunSource(run_id=run.id, company_id=1, verified_urls='["https://j/a"]')
    )
    session.commit()
    row = session.query(RunSource).one()
    assert row.verified_urls == '["https://j/a"]'
