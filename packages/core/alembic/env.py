"""Alembic migration environment (Phase 3.8f pre-task, decided 2026-09-11).

Wired programmatically from ``jobsearch_core.db.migrations`` so the URL always
matches the app's configured database. Brand-new databases are handled by
``create_all`` + ``stamp head`` there, not by these scripts; existing databases
replay migrations from their (stamped or inferred) current revision.
"""

from __future__ import annotations

from sqlalchemy import engine_from_config, pool

from alembic import context
from jobsearch_core.db.models import Base

config = context.config

# The app always injects the URL programmatically (see db/migrations.py).
if not config.get_main_option("sqlalchemy.url"):
    raise RuntimeError("sqlalchemy.url must be set before running migrations")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_name="sqlite",
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
