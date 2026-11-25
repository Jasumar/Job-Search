"""Add companies.recipe_state and companies.recipe_trials (F1a attempt tracking).

Attempt tracking is state, not time (owner decision, 2026-10-18): a company
whose recipe fails three trials is set to ``recipe_fail`` and stays there until
a manual rerun clears it. ``recipe_state`` is ``ok`` or ``recipe_fail``.

Revision ID: 0011_company_recipe_state
Revises: 0010_source_runs
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0011_company_recipe_state"
down_revision: str | None = "0010_source_runs"
branch_labels: str | None = None
depends_on: str | None = None

_COMPANIES = "companies"
_COLUMNS = ("recipe_state", "recipe_trials")


def upgrade() -> None:
    connection = op.get_bind()
    existing = {col["name"] for col in inspect(connection).get_columns(_COMPANIES)}

    if "recipe_state" not in existing:
        op.add_column(
            _COMPANIES,
            sa.Column(
                "recipe_state",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'ok'"),
            ),
        )
    if "recipe_trials" not in existing:
        op.add_column(
            _COMPANIES,
            sa.Column(
                "recipe_trials",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    connection = op.get_bind()
    existing = {col["name"] for col in inspect(connection).get_columns(_COMPANIES)}
    for column in _COLUMNS:
        if column in existing:
            with op.batch_alter_table(_COMPANIES) as batch:
                batch.drop_column(column)
