"""Add run_sources.entry_trace: the evidence behind an entry-resolution miss.

A `careers_search` row that resolved to nothing carried only a sentence in
`error`; the descent's actual work — which candidates were fetched, what they
scored, which shortlist was ranked — lived in the log and was lost with it.
The demo review needs that evidence on the row, so the report's JSON is stored
here.

NULL is meaningful and expected: rows written before this revision, and
`name_collect` rows (which have no entry descent at all), stay NULL. The column
is a JSON payload in TEXT, like `run_sources.verified_urls` and the profile
list columns — no database-level JSON type, because SQLite's JSON1 support is
a compile option this codebase does not depend on.

Revision ID: 0016_run_source_entry_trace
Revises: 0015_companies_run_id
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0016_run_source_entry_trace"
down_revision: str | None = "0015_companies_run_id"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "run_sources"
_COLUMN = "entry_trace"


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
