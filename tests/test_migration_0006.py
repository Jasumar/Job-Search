"""Gap 12 migration: jobs.company_id and its backfill.

The backfill runs once against a database that already holds rows. Its
correctness property is conservative: it links a job only when the careers
URL matches exactly or the normalized company name matches exactly, and it
refuses to guess when a name is ambiguous. Rows it cannot prove stay NULL.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from jobsearch_core.db.models import Company, Job
from jobsearch_core.db.session import get_engine, init_db

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "packages/core/alembic/versions/0006_jobs_company_id.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0006", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job(url: str, **kwargs) -> Job:
    """A minimally valid Job. raw_title is NOT NULL; everything the backfill
    reads (source_url, normalized_company) is optional."""
    return Job(
        url=url,
        raw_title=f"title {url}",
        raw_company=kwargs.pop("raw_company", f"company {url}"),
        raw_location=kwargs.pop("raw_location", "Helsinki, Finland"),
        **kwargs,
    )


def _seed(session) -> None:
    session.add(Company(name="Reaktor", careers_url="https://reaktor.fi/careers"))
    session.add(Company(name="Wolt"))
    # Two rows normalizing to the same name: a name-only lookup must not pick.
    session.add(Company(name="Ambiguous Co"))
    session.add(Company(name="Ambiguous Co."))
    session.commit()


def test_backfill_links_by_careers_url_and_by_name(db_url, session):
    _seed(session)
    session.add_all(
        [
            # Scheme, www. and trailing slash must all be insignificant.
            _job(
                "u1",
                source_url="https://www.reaktor.fi/careers/",
                normalized_company="reaktor",
            ),
            # Different URL, but the name proves the owner.
            _job(
                "u2",
                source_url="https://boards.example.com/wolt",
                normalized_company="wolt",
            ),
        ]
    )
    session.commit()

    migration = _load_migration()
    with get_engine().begin() as conn:
        linked, ambiguous = migration._backfill(conn)
    assert linked == 2
    assert ambiguous == 1  # the Ambiguous Co / Ambiguous Co. pair

    by_url = {j.url: j for j in session.query(Job).all()}
    assert by_url["u1"].company_id is not None
    assert by_url["u2"].company_id is not None


def test_backfill_leaves_unprovable_rows_null(db_url, session):
    _seed(session)
    session.add_all(
        [
            # Ambiguous name: must stay NULL rather than be attributed.
            _job("amb", source_url="https://x.example", normalized_company="ambiguous co"),
            # No company record at all.
            _job("orphan", source_url="https://y.example", normalized_company="ghost holdings"),
            # No source URL and no recognizable name.
            _job("bare", source_url=None, normalized_company=None),
        ]
    )
    session.commit()

    migration = _load_migration()
    with get_engine().begin() as conn:
        migration._backfill(conn)

    assert all(j.company_id is None for j in session.query(Job).all())


def test_backfill_is_idempotent(db_url, session):
    """Re-running must not re-link or corrupt already-linked rows."""
    _seed(session)
    session.add(_job("u1", source_url="https://reaktor.fi/careers", normalized_company="reaktor"))
    session.commit()

    migration = _load_migration()
    with get_engine().begin() as conn:
        first, _ = migration._backfill(conn)
        second, _ = migration._backfill(conn)
    assert first == second == 1

    rows = session.query(Job).all()
    assert len(rows) == 1
    assert rows[0].company_id is not None


def test_upgrade_adds_column_foreign_key_and_index(db_url):
    """The end-to-end migration path, run by init_db on a fresh database."""
    init_db(db_url)
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    assert "company_id" in {c["name"] for c in inspector.get_columns("jobs")}
    fks = [f for f in inspector.get_foreign_keys("jobs") if f["constrained_columns"] == ["company_id"]]
    assert fks and fks[0]["referred_table"] == "companies"
    assert any(i["name"] == "ix_jobs_company_id" for i in inspector.get_indexes("jobs"))
