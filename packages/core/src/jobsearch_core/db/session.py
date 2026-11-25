from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from jobsearch_core.db.migrations import run_migrations
from jobsearch_core.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None

# RF10: the API and the worker share one SQLite file. WAL lets a reader proceed
# while the worker writes; busy_timeout makes a second writer wait instead of
# failing at once with "database is locked". Milliseconds.
SQLITE_BUSY_TIMEOUT_MS = 10_000


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Set the per-connection pragmas on every new SQLite connection.

    ``busy_timeout`` goes first: switching the journal mode takes a lock, and the
    very first connections of two processes can race for it. ``journal_mode`` is
    stored in the database file, so it persists; an in-memory database ignores it.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def init_db(database_url: str, *, echo: bool = False, ensure_schema: bool = True) -> None:
    """Bind the process-wide engine to ``database_url`` and bring its schema up.

    ``ensure_schema=False`` is for a database already built by :func:`init_db` —
    a test copying a migrated template, say. It only rebinds the engine; no
    ``create_all`` and no migration run.
    """
    global _engine, _SessionFactory
    engine_options = {"echo": echo}
    if database_url.startswith("sqlite"):
        engine_options["poolclass"] = NullPool
        # A fresh clone has no data/ directory; SQLite creates the file, not its folder.
        db_path = make_url(database_url).database
        if db_path and db_path != ":memory:" and not db_path.startswith("file:"):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(database_url, **engine_options)
    if database_url.startswith("sqlite"):
        event.listen(_engine, "connect", _configure_sqlite_connection)
    _SessionFactory = sessionmaker(bind=_engine)
    if not ensure_schema:
        return
    # Phase 3.8f pre-task (decided 2026-09-11): brand-new databases are built
    # from the models (create_all) and stamped to the Alembic baseline;
    # existing databases replay pending migrations. The ad-hoc startup
    # upgrade path is retired — schema changes go through Alembic only.
    Base.metadata.create_all(_engine)
    run_migrations(_engine, database_url)


def get_session() -> Session:
    if _SessionFactory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _SessionFactory()


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine
