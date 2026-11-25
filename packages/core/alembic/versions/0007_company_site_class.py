"""Add companies.site_class and companies.site_class_evidence (Phase 4.3).

The website-type classifier records its verdict per company row: the top
class letter (A-E) and the full ranked evidence as JSON. Both columns are
nullable: rows classified before this migration, or never classified, stay
NULL rather than receiving a guessed class.

Revision ID: 0007_company_site_class
Revises: 0006_jobs_company_id
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0007_company_site_class"
down_revision: str | None = "0006_jobs_company_id"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS = ("site_class", "site_class_evidence")


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    existing = {c["name"] for c in inspector.get_columns("companies")}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column("companies", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    existing = {c["name"] for c in inspector.get_columns("companies")}
    for name in _COLUMNS:
        if name in existing:
            op.drop_column("companies", name)
