"""M1 migration: sources, run_sources, search_cache (plan-source-runs §Model).

Properties: upgrade is idempotent; downgrade drops only these three tables;
a run_sources row has exactly one owner; a search_cache (company, query) pair is
unique; saved sources survive deletion of the run that found them.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company, JobRun, RunSource, SearchCache, Source
from jobsearch_core.db.session import get_engine, init_db
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "packages/core/alembic/versions/0010_source_runs.py"
)
_NEW_TABLES = {"sources", "run_sources", "search_cache"}
_NOW = datetime(2026, 10, 17)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0010", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_idempotent_and_downgrade_keeps_other_tables(db_url, session):
    init_db(db_url)
    engine = get_engine()
    module = _load_migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op
        names = set(inspect(conn).get_table_names())
        assert _NEW_TABLES <= names
        with Operations.context(ctx):
            module.downgrade()
        names = set(inspect(conn).get_table_names())
        assert not (_NEW_TABLES & names)
        assert {"companies", "company_recipes", "job_runs"} <= names


def test_run_source_owner_is_enforced(session):
    company = Company(name="Nordea", careers_url=None)
    run = JobRun(run_type="careers_search", status="queued")
    source = Source(url="https://example.com/list", created_at=_NOW, updated_at=_NOW)
    session.add_all([company, run, source])
    session.commit()

    session.add(RunSource(run_id=run.id, company_id=company.id, source_id=source.id))
    with pytest.raises(IntegrityError):  # both owners set
        session.commit()
    session.rollback()

    session.add(RunSource(run_id=run.id))
    with pytest.raises(IntegrityError):  # no owner
        session.commit()
    session.rollback()

    session.add(RunSource(run_id=run.id, company_id=company.id, status="queued"))
    session.commit()
    assert session.query(RunSource).count() == 1


def test_run_source_status_is_constrained(session):
    run = JobRun(run_type="name_collect", status="queued")
    session.add(run)
    session.commit()
    session.add(RunSource(run_id=run.id, status="done", source_id=None, company_id=None))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_search_cache_is_unique_per_company_and_query(session):
    company = Company(name="Acme")
    session.add(company)
    session.commit()
    session.add(SearchCache(company_id=company.id, query="Acme careers", payload="[]", fetched_at=_NOW))
    session.commit()
    session.add(SearchCache(company_id=company.id, query="Acme careers", payload="[]", fetched_at=_NOW))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_saved_source_survives_run_deletion(session):
    run = JobRun(run_type="name_collect", status="completed")
    source = Source(url="https://example.com/saved", created_at=_NOW, updated_at=_NOW)
    session.add_all([run, source])
    session.commit()
    session.add(RunSource(run_id=run.id, source_id=source.id))
    session.commit()

    session.query(RunSource).delete()
    session.delete(run)
    session.commit()
    assert session.query(Source).filter_by(url="https://example.com/saved").count() == 1
