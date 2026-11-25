"""Alembic wiring for the jobsearch database (Phase 3.8f pre-task).

Decided 2026-09-11 (master_plan.md "User Profile in DB"):

- **Brand-new databases:** ``Base.metadata.create_all`` (unchanged behavior)
  followed by ``alembic stamp head`` — the schema is the models, and the
  revision history is recorded so later migrations start from the right place.
- **Existing databases:** replay any pending migrations from their current
  revision. A database that predates Alembic (no ``alembic_version`` table) is
  stamped to the pre-user-profile baseline first: the ad-hoc startup upgrades
  in ``db.session`` already brought it to the current pre-3.8f schema, so its
  first real migration is the ``user_profile`` one.
- **Ad-hoc startup upgrades are retired:** only migrations change the schema
  from here on.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from alembic import command
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

# Head revision before the first real migration (the pre-3.8f schema state
# produced by create_all + the retired ad-hoc startup upgrades).
BASELINE_REVISION = "0001_baseline"

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "alembic"


def _alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _current_revision(connection) -> str | None:
    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return None
    from sqlalchemy import text

    rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    return rows[0][0] if rows else None


def _synced_url(database_url: str) -> str:
    """Return a URL Alembic can sync from (SQLite needs no special handling)."""
    return database_url


@lru_cache(maxsize=1)
def _head_revisions() -> frozenset[str]:
    """Head revision ids of the migration scripts (cached: parsing is not free)."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    return frozenset(script.get_heads())


def run_migrations(engine: Engine, database_url: str) -> None:
    """Bring the database to head, handling legacy (pre-Alembic) databases."""
    with engine.connect() as connection:
        current = _current_revision(connection)
    if current is not None and current in _head_revisions():
        # Already at head: starting the Alembic environment costs ~1s on SQLite,
        # and this runs on every init_db (i.e. every CLI invocation and every
        # DB-backed test), so the common case is answered with one query.
        logger.debug("Alembic: already at head (%s)", current)
        return
    if current is None:
        # Pre-Alembic database (or fresh): mark the baseline schema state so
        # only real migrations replay. For a fresh database create_all has
        # already built everything, so stamping is exactly right; for a legacy
        # database the retired ad-hoc upgrades already did the same job.
        command.stamp(_alembic_config(database_url), BASELINE_REVISION)
        logger.info("Alembic: stamped baseline revision %s", BASELINE_REVISION)
    config = _alembic_config(_synced_url(database_url))
    command.upgrade(config, "head")


def upgrade_head(database_url: str) -> None:
    """CLI/utility entry point: migrate a database URL to head."""
    config = _alembic_config(_synced_url(database_url))
    command.upgrade(config, "head")
