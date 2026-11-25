from __future__ import annotations

from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_engine, get_session, init_db
from sqlalchemy import create_engine, inspect, text


def _create_legacy_discovery_queue(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE discovery_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    country TEXT,
                    careers_url TEXT,
                    source_url TEXT,
                    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
                    resolution_page_type TEXT NOT NULL DEFAULT 'unknown',
                    verification_status TEXT NOT NULL DEFAULT 'unverified',
                    verified_at DATETIME,
                    ats_platform TEXT,
                    source_query TEXT,
                    triage_decision TEXT,
                    triage_source_type TEXT,
                    triage_confidence REAL,
                    triage_evidence TEXT,
                    triage_reason TEXT,
                    identity_name TEXT,
                    identity_confidence REAL,
                    identity_evidence TEXT,
                    identity_reason TEXT,
                    tech_employer TEXT,
                    followup_query TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    discovered_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO discovery_queue (name, country, careers_url, status, tech_employer) "
                "VALUES (:name, :country, :careers_url, :status, :tech_employer)"
            ),
            [
                {"name": "PendingCo", "country": "FI", "careers_url": "https://p.example/careers",
                 "status": "pending", "tech_employer": "yes"},
                {"name": "AcceptedCo", "country": "EE", "careers_url": "https://a.example/careers",
                 "status": "accepted", "tech_employer": "yes"},
                {"name": "RejectedCo", "country": None, "careers_url": None,
                 "status": "rejected", "tech_employer": "no"},
            ],
        )
    engine.dispose()


def test_migration_copies_discovery_queue_rows_and_preserves_status(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    _create_legacy_discovery_queue(db_url)

    init_db(db_url)
    engine = get_engine()
    inspector = inspect(engine)
    assert "discovery_queue" not in inspector.get_table_names()
    assert "companies" in inspector.get_table_names()

    session = get_session()
    rows = session.query(Company).order_by(Company.id).all()
    session.close()
    assert [c.name for c in rows] == ["PendingCo", "AcceptedCo", "RejectedCo"]
    assert {c.status for c in rows} == {"pending", "accepted", "rejected"}
    # Statuses are preserved verbatim; nothing is auto-promoted to known.
    assert not any(c.status == "known" for c in rows)
    accepted = next(c for c in rows if c.name == "AcceptedCo")
    assert accepted.careers_url == "https://a.example/careers"
    assert accepted.country == "EE"
    assert accepted.employer_domain == "yes"
    assert accepted.notes == ""
    engine.dispose()


def test_migration_is_idempotent(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'idem.db'}"
    _create_legacy_discovery_queue(db_url)

    init_db(db_url)
    init_db(db_url)  # second init must be a no-op

    engine = get_engine()
    inspector = inspect(engine)
    assert "discovery_queue" not in inspector.get_table_names()

    session = get_session()
    count = session.query(Company).count()
    session.close()
    assert count == 3
    engine.dispose()


def test_rename_tech_employer_column_to_employer_domain(tmp_path):
    """A Phase 3.6 `companies` table with the old `tech_employer` column is renamed
    in place to `employer_domain`, preserving values (Phase 3.6a)."""
    db_url = f"sqlite:///{tmp_path / 'renamed.db'}"
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    country TEXT,
                    careers_url TEXT,
                    source_query TEXT,
                    ats_platform TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    tech_employer TEXT,
                    discovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO companies (name, country, careers_url, status, tech_employer) "
                "VALUES (:name, :country, :careers_url, :status, :tech_employer)"
            ),
            [
                {"name": "Acme", "country": "FI", "careers_url": "https://a.example/careers",
                 "status": "known", "tech_employer": "no"},
            ],
        )
    engine.dispose()

    init_db(db_url)
    engine = get_engine()
    inspector = inspect(engine)
    column_names = {column["name"] for column in inspector.get_columns("companies")}
    assert "employer_domain" in column_names
    assert "tech_employer" not in column_names

    session = get_session()
    company = session.query(Company).first()
    session.close()
    assert company.employer_domain == "no"
    engine.dispose()
