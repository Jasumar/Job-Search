"""Create company_recipes: recorded scrape paths for class F (plan-scraping §Recipe authoring).

One table serves both recipe scopes. ``company_careers`` recipes belong to a
company's careers/open-positions page (``company_id`` set). ``m1_url`` recipes
belong to a URL re-run by the M1 scheduler (``target_url`` set). The CHECK
constraint keeps each row to exactly one owner shape.

``body`` is the JSON-serialised recipe (kind-specific, validated in code before
use). ``verification`` is the JSON replay result; it stays NULL until replay
matches. A row with status ``candidate`` or ``stale`` is never run as verified.

Revision ID: 0009_company_recipes
Revises: 0008_company_xhr_capture
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0009_company_recipes"
down_revision: str | None = "0008_company_xhr_capture"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "company_recipes"
_INDEX_COMPANY = "ix_company_recipes_company_id"
_INDEX_TARGET = "ix_company_recipes_target_url"


def upgrade() -> None:
    connection = op.get_bind()
    if _TABLE in set(inspect(connection).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id"),
            nullable=True,
        ),
        sa.Column("target_url", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'candidate'")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("verification", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('company_careers', 'm1_url')", name="ck_company_recipes_scope"
        ),
        sa.CheckConstraint("kind IN ('api', 'dom')", name="ck_company_recipes_kind"),
        sa.CheckConstraint(
            "status IN ('candidate', 'verified', 'stale')",
            name="ck_company_recipes_status",
        ),
        sa.CheckConstraint(
            "(scope = 'company_careers' AND company_id IS NOT NULL)"
            " OR (scope = 'm1_url' AND target_url IS NOT NULL)",
            name="ck_company_recipes_owner",
        ),
    )
    op.create_index(_INDEX_COMPANY, _TABLE, ["company_id"])
    op.create_index(_INDEX_TARGET, _TABLE, ["target_url"])


def downgrade() -> None:
    connection = op.get_bind()
    if _TABLE in set(inspect(connection).get_table_names()):
        op.drop_index(_INDEX_TARGET, table_name=_TABLE)
        op.drop_index(_INDEX_COMPANY, table_name=_TABLE)
        op.drop_table(_TABLE)
