"""Repair the missing company_xhr_capture.replay_reason column (schema drift).

0008 creates the table with ``replay_reason``, but it returns early when the
table already exists. A database whose table was created before 2026-10-14
(the commit that introduced the column) therefore has the table without the
column, and 0013 added only its own four columns. The result is a database
stamped at head whose table cannot accept a capture row:

    table company_xhr_capture has no column named replay_reason

This revision is the repair: idempotent, so it is a no-op on databases built
by ``create_all`` and a one-column add on drifted ones. Nothing is backfilled;
the column is an explanation for a replay verdict and a guessed value would
read as evidence.

Revision ID: 0018_capture_replay_reason
Revises: 0017_run_source_author_trace
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0018_capture_replay_reason"
down_revision: str | None = "0017_run_source_author_trace"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "company_xhr_capture"
_COLUMN = "replay_reason"


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
