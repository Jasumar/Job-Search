"""Add companies.open_positions_url and its descent provenance (4.0d).

The entry a landing page resolves to is persisted on its own, with the page
and link text it was reached from, so the human gate can show *why* the entry
is what it is and the next scrape starts at the page that lists jobs. NULL
means "never resolved": scraping then uses ``careers_url``.

Revision ID: 0012_open_positions_entry
Revises: 0011_company_recipe_state
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0012_open_positions_entry"
down_revision: str | None = "0011_company_recipe_state"
branch_labels: str | None = None
depends_on: str | None = None

_COMPANIES = "companies"
_COLUMNS = (
    "open_positions_url",
    "open_positions_landing_url",
    "open_positions_link_text",
)


def upgrade() -> None:
    connection = op.get_bind()
    existing = {col["name"] for col in inspect(connection).get_columns(_COMPANIES)}

    for column in _COLUMNS:
        if column not in existing:
            op.add_column(_COMPANIES, sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    existing = {col["name"] for col in inspect(connection).get_columns(_COMPANIES)}
    for column in _COLUMNS:
        if column in existing:
            with op.batch_alter_table(_COMPANIES) as batch:
                batch.drop_column(column)
