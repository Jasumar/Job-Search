"""Add recipe_flags: posting-existence gate answers keyed by recipe body hash (R3).

plan-scraping.md §Recipe closure R3: the gate's verdict, the offending item URLs
with one sentence each, the model id, and whether the verdict stopped the run.
Keyed by ``body_sha256`` so a re-authored body never inherits the previous
body's analysis; ``recipe_id`` is nullable provenance only. Rows are a history,
one per gate event, so a repeat "none" is visible.

No backfill: a flag is an observation, and a guessed one would read as evidence.

Revision ID: 0014_recipe_flags
Revises: 0013_recipe_evidence
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0014_recipe_flags"
down_revision: str | None = "0013_recipe_evidence"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "recipe_flags"
_INDEXES = ("ix_recipe_flags_body_sha256", "ix_recipe_flags_recipe_id")


def upgrade() -> None:
    connection = op.get_bind()
    if inspect(connection).has_table(_TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("company_recipes.id"), nullable=True),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("analysis", sa.Text(), nullable=False),
        sa.Column("stopped", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("verdict IN ('none', 'postings')", name="ck_recipe_flags_verdict"),
    )
    op.create_index("ix_recipe_flags_body_sha256", _TABLE, ["body_sha256"])
    op.create_index("ix_recipe_flags_recipe_id", _TABLE, ["recipe_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if not inspect(connection).has_table(_TABLE):
        return
    for name in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
