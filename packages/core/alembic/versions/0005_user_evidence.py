"""Create user_evidence and move contact fields onto user_profile (Phase 4.1a).

Retires ``data/evidence.yaml``: the CV facts move into a new single-row
``user_evidence`` document and the contact details move onto the existing
``user_profile`` row. No data is copied here — the one-time import is the
explicit ``evidence import`` CLI command (decided 2026-09-29), so a failure
there cannot leave half-migrated truth in the schema.

Revision ID: 0005_user_evidence
Revises: 0004_profile_role_families
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0005_user_evidence"
down_revision: str | None = "0004_profile_role_families"
branch_labels: str | None = None
depends_on: str | None = None

_PROFILE_COLUMNS = (
    "location",
    "citizenship",
    "linkedin",
    "gitlab",
    "email",
    "phone",
    "visa_required",
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if "user_evidence" not in tables:
        op.create_table(
            "user_evidence",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("education", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("experience", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("projects", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("skills", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("languages", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("linked_year", sa.Integer(), nullable=True),
            sa.Column(
                "additional_experience",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing = {col["name"] for col in inspector.get_columns("user_profile")}
    for name in _PROFILE_COLUMNS:
        if name in existing:
            continue
        if name == "visa_required":
            op.add_column(
                "user_profile",
                sa.Column(
                    "visa_required",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
            )
        else:
            op.add_column(
                "user_profile",
                sa.Column(name, sa.Text(), nullable=False, server_default=""),
            )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    existing = {col["name"] for col in inspector.get_columns("user_profile")}
    for name in reversed(_PROFILE_COLUMNS):
        if name in existing:
            op.drop_column("user_profile", name)
    if "user_evidence" in set(inspector.get_table_names()):
        op.drop_table("user_evidence")
