"""Add companies.run_id: the run whose listing produced a candidate (demo gate).

The review screen has to name the run and listing a pending row came from
("these came from that page"), so ``name_collect`` stamps the run id on every
candidate it inserts. NULL is meaningful and expected: discovery/search-derived
rows have no listing behind them, and every row written before this revision
stays NULL rather than being guessed at.

The FK is ``job_runs.id`` with ``ON DELETE SET NULL`` in the models, which is
what a fresh database gets from ``create_all``. This migration adds the column
and its index only, exactly like 0006 did for ``jobs.company_id``: SQLite cannot
add a foreign key to an existing table without rebuilding it, and the rebuild
buys nothing today because nothing in this codebase turns on
``PRAGMA foreign_keys`` — SQLite does not enforce the constraint at all. The
practical rule it encodes is held by application code: deleting a run never
deletes a company.

Revision ID: 0015_companies_run_id
Revises: 0014_recipe_flags
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0015_companies_run_id"
down_revision: str | None = "0014_recipe_flags"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "companies"
_COLUMN = "run_id"
_INDEX = "ix_companies_run_id"


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))

    if _INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    if _INDEX in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
