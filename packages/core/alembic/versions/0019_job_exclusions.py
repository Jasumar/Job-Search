"""Create job_exclusions: fingerprints of postings a human deleted.

Deleting a job row removes the only copy of its ``dedup_hash``, which is what
duplicate detection reads, so the next scrape would re-add the same posting.
This table keeps the fingerprint (unique), the URL and the title that were
deleted, so the ingest path can skip them and the row stays as evidence of the
decision.

No backfill: exclusions start empty; posts deleted before this revision were
already re-addable and there is nothing to reconstruct.

Revision ID: 0019_job_exclusions
Revises: 0018_capture_replay_reason
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0019_job_exclusions"
down_revision: str | None = "0018_capture_replay_reason"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "job_exclusions"


def upgrade() -> None:
    connection = op.get_bind()
    if _TABLE in set(inspect(connection).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dedup_hash", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("raw_title", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dedup_hash", name="uq_job_exclusions_dedup_hash"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if _TABLE in set(inspect(connection).get_table_names()):
        op.drop_table(_TABLE)
