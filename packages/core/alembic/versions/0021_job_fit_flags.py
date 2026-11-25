"""Jobs carry fit flags, not a rejected status (3.8c M0).

``jobs.fit_flags`` is a JSON list of ``{"reason", "detail"}``: the ingest filter
hits, recorded as facts about the posting. ``jobs.fit_components`` holds the
five match components of the last scoring. ``rejected_by`` / ``rejected_reason``
move into the flags and are dropped; every ``rejected`` job becomes ``new``, and
its stored description makes it scoreable without a new fetch.

Revision ID: 0021_job_fit_flags
Revises: 0020_job_profile_failed
Create Date: 2026-11-23
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0021_job_fit_flags"
down_revision: str | None = "0020_job_profile_failed"
branch_labels: str | None = None
depends_on: str | None = None


def _columns() -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns("jobs")}


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("jobs") as batch:
        if "fit_flags" not in columns:
            batch.add_column(sa.Column("fit_flags", sa.Text(), nullable=True))
        if "fit_components" not in columns:
            batch.add_column(sa.Column("fit_components", sa.Text(), nullable=True))

    bind = op.get_bind()
    if "rejected_by" in columns:
        rows = bind.execute(
            sa.text(
                "SELECT id, rejected_by, rejected_reason FROM jobs "
                "WHERE rejected_by IS NOT NULL OR rejected_reason IS NOT NULL"
            )
        ).fetchall()
        for job_id, reason, detail in rows:
            flags = json.dumps([{"reason": reason, "detail": detail}], ensure_ascii=False)
            bind.execute(
                sa.text("UPDATE jobs SET fit_flags = :flags WHERE id = :id"),
                {"flags": flags, "id": job_id},
            )
    bind.execute(sa.text("UPDATE jobs SET status = 'new' WHERE status = 'rejected'"))

    with op.batch_alter_table("jobs") as batch:
        if "rejected_by" in columns:
            batch.drop_column("rejected_by")
        if "rejected_reason" in columns:
            batch.drop_column("rejected_reason")


def downgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("jobs") as batch:
        if "rejected_by" not in columns:
            batch.add_column(sa.Column("rejected_by", sa.Text(), nullable=True))
        if "rejected_reason" not in columns:
            batch.add_column(sa.Column("rejected_reason", sa.Text(), nullable=True))
        if "fit_flags" in columns:
            batch.drop_column("fit_flags")
        if "fit_components" in columns:
            batch.drop_column("fit_components")
