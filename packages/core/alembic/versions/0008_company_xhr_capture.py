"""Create company_xhr_capture: per-company XHR replay evidence (Phase 4.3).

One row per observed JSON API call that carries a company's job list. The
capture records what the browser called, the size-capped sample of what came
back, and whether a plain PoliteClient replay (no browser, no cookies)
reproduces it. Cookie and Authorization values are never stored.

Rows are evidence, not verdicts: a row with ``replay_matches`` = false or
NULL must not make a company class A. Class A is decided by the classifier.

Revision ID: 0008_company_xhr_capture
Revises: 0007_company_site_class
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0008_company_xhr_capture"
down_revision: str | None = "0007_company_site_class"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "company_xhr_capture"


def upgrade() -> None:
    connection = op.get_bind()
    # Early return: a table created by an older revision is left untouched.
    # That is why 0018_capture_replay_reason exists — databases whose table
    # predates 2026-10-14 have no `replay_reason` column, and this revision
    # will never add it to them.
    if _TABLE in set(inspect(connection).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("response_bytes", sa.Integer(), nullable=False),
        sa.Column("sample_titles", sa.Text(), nullable=True),
        sa.Column("replay_matches", sa.Boolean(), nullable=True),
        sa.Column("replay_reason", sa.Text(), nullable=True),
        sa.Column("class_a_candidate", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_company_xhr_capture_company_id", _TABLE, ["company_id"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    if _TABLE in set(inspect(connection).get_table_names()):
        op.drop_index("ix_company_xhr_capture_company_id", table_name=_TABLE)
        op.drop_table(_TABLE)
