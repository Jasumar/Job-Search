"""Migration 0021: fit flags replace the rejected job status (3.8c M0)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "packages/core/alembic/versions/0021_job_fit_flags.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0021", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chains_onto_0020():
    module = _load()
    assert module.revision == "0021_job_fit_flags"
    assert module.down_revision == "0020_job_profile_failed"


def test_rejected_rows_become_new_with_their_reason_as_a_flag(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    module = _load()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
            "rejected_by TEXT, rejected_reason TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO jobs VALUES (1, 'rejected', 'explicit_senior_title', 'matched pattern in title'),"
            " (2, 'new', NULL, NULL)"
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # idempotent

        columns = {c["name"] for c in inspect(conn).get_columns("jobs")}
        assert {"fit_flags", "fit_components"} <= columns
        assert not {"rejected_by", "rejected_reason"} & columns

        rows = {r.id: r for r in conn.exec_driver_sql("SELECT id, status, fit_flags FROM jobs")}
        assert rows[1].status == "new"
        assert json.loads(rows[1].fit_flags) == [
            {"reason": "explicit_senior_title", "detail": "matched pattern in title"}
        ]
        assert rows[2].status == "new" and rows[2].fit_flags is None
