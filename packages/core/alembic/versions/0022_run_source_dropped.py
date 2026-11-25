"""Add run_sources.dropped: the links the scrape filter judged not postings.

The generic scrape drops links that are not postings before any detail fetch.
Every drop is recorded here (counts, reasons, up to five examples) so a wrong
drop is visible on screen. NULL means nothing was dropped or the row was not
judged (recipe replay, sitemap, name_collect). The column is a JSON payload in
TEXT, like `author_trace`. The status CHECK is not touched: `zero_yield`
already exists.

Revision ID: 0022_run_source_dropped
Revises: 0021_job_fit_flags
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0022_run_source_dropped"
down_revision: str | None = "0021_job_fit_flags"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "run_sources"
_COLUMN = "dropped"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
