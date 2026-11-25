"""Add the user_profile table (Phase 3.8f backend).

Revision ID: 0003_user_profile
Revises: 0002_legacy_reconcile
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0003_user_profile"
down_revision: str | None = "0002_legacy_reconcile"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ``Base.metadata.create_all`` runs before migrations in ``init_db``, so a
    # fresh database already carries this table; the create is skipped there.
    # On a legacy database create_all has likewise just added it (empty), which
    # matches the retired ad-hoc path's create-missing-tables behavior.
    connection = op.get_bind()
    inspector = inspect(connection)
    if "user_profile" in inspector.get_table_names():
        return
    op.create_table(
        "user_profile",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("education", sa.Text(), nullable=False, server_default=""),
        sa.Column("experience", sa.Text(), nullable=False, server_default=""),
        sa.Column("skills", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("job_preferences", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("target_countries", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("target_cities", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("full_time", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("part_time", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("onsite", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("hybrid", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("remote", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("cv_text", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("user_profile")
