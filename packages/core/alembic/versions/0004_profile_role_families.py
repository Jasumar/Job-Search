"""Add the role_families JSON list to user_profile (Phase 3.8f reconciliation).

The wizard's role-family chip selections persist on the profile (user-approved
decision 2026-09-11: ``role_families`` JSON string-list column) so discovery
can map them to segment templates at run start.

Revision ID: 0004_profile_role_families
Revises: 0003_user_profile
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0004_profile_role_families"
down_revision: str | None = "0003_user_profile"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = {col["name"] for col in inspector.get_columns("user_profile")}
    if "role_families" in columns:
        return
    op.add_column(
        "user_profile",
        sa.Column("role_families", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("user_profile", "role_families")
