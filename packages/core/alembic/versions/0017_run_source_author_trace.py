"""Add run_sources.author_trace: the evidence behind an M1 recipe authoring.

An M1 listing row that could not be authored carried one sentence in `error`
(the last trial's reason), and a row authored on the third try carried nothing:
which bodies the model proposed, which trial failed and why was in the log
only. The owner reads the run screen when a listing yields nothing, so the
trials are stored on the row, in the same shape `entry_trace` uses.

NULL is meaningful and expected: rows written before this revision, and every
`careers_search` row (a company resolves an entry, it never authors a listing).
The column is a JSON payload in TEXT, like `entry_trace` and `verified_urls`.

Revision ID: 0017_run_source_author_trace
Revises: 0016_run_source_entry_trace
Create Date: 2026-11-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0017_run_source_author_trace"
down_revision: str | None = "0016_run_source_entry_trace"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "run_sources"
_COLUMN = "author_trace"


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
