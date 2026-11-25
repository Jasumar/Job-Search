"""Migration 0012: companies.open_positions_url and its descent provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.db.models import Company
from sqlalchemy import create_engine, inspect, text

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0012_open_positions_entry.py"
)
NEW_COLUMNS = {
    "open_positions_url",
    "open_positions_landing_url",
    "open_positions_link_text",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0012", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_company_has_no_resolved_entry(session):
    company = Company(name="Acme", careers_url="https://acme.example/careers")
    session.add(company)
    session.commit()

    assert company.open_positions_url is None
    assert company.open_positions_landing_url is None
    assert company.open_positions_link_text is None


def test_entry_survives_a_round_trip(session):
    company = Company(
        name="Acme",
        status="accepted",
        careers_url="https://acme.example/careers",
        open_positions_url="https://acme.example/avoimet-tyopaikat",
        open_positions_landing_url="https://acme.example/careers",
        open_positions_link_text="Avoimet työpaikat",
    )
    session.add(company)
    session.commit()
    session.expire_all()

    stored = session.query(Company).filter(Company.name == "Acme").one()
    assert stored.open_positions_url == "https://acme.example/avoimet-tyopaikat"
    assert stored.open_positions_link_text == "Avoimet työpaikat"


def test_migration_adds_and_drops_entry_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'entry.sqlite3'}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO companies (id, name) VALUES (1, 'Acme')"))
        conn.commit()

        ctx = MigrationContext.configure(conn)
        module = _load_migration()
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # second run must be a no-op

        cols = {c["name"] for c in inspect(conn).get_columns("companies")}
        assert NEW_COLUMNS <= cols
        # Existing rows stay NULL: unresolved is the default, not a backfill.
        row = conn.execute(
            text(
                "SELECT open_positions_url, open_positions_landing_url, "
                "open_positions_link_text FROM companies"
            )
        ).one()
        assert tuple(row) == (None, None, None)

        with Operations.context(ctx):
            module.downgrade()
        cols = {c["name"] for c in inspect(conn).get_columns("companies")}
        assert not (NEW_COLUMNS & cols)
        assert "companies" in inspect(conn).get_table_names()
