"""Add jobs.profile_failed_at: the profile-extraction failure marker (RF8).

An invalid model answer for a description repeats on every run. The marker lets
extraction skip the job until a manual rerun clears it.

No backfill: existing rows start unmarked.

Revision ID: 0020_job_profile_failed
Revises: 0019_job_exclusions
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0020_job_profile_failed"
down_revision: str | None = "0019_job_exclusions"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMN = "profile_failed_at"


def _has_column() -> bool:
    return _COLUMN in {c["name"] for c in inspect(op.get_bind()).get_columns("jobs")}


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column(_COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column(_COLUMN)
