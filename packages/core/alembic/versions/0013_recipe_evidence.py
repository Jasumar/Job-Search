"""Add XHR capture evidence columns and run_sources.verified_urls (R1).

plan-scraping.md §Recipe closure R1: the drift baseline needs the capture row to
carry what it observed (``content_type``, ``body_sha256``, ``body_truncated``)
and to be a timeline rather than a snapshot (``capture_batch``, one value per
render). Source runs store the verified posting URL set they produced in
``run_sources.verified_urls`` so run N can be compared to run N-1 on URLs, not
counts; ``sources.last_verified_count`` already exists (0010) and is derived
from that set.

No backfill: rows written before this revision have no body hash and no batch,
and a guessed value would read as evidence. ``body_truncated`` and
``capture_batch`` are NOT NULL with server defaults, which SQLite applies to
existing rows.

Revision ID: 0013_recipe_evidence
Revises: 0012_open_positions_entry
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0013_recipe_evidence"
down_revision: str | None = "0012_open_positions_entry"
branch_labels: str | None = None
depends_on: str | None = None

_CAPTURE = "company_xhr_capture"
_RUN_SOURCES = "run_sources"

_CAPTURE_COLUMNS = ("content_type", "body_sha256", "body_truncated", "capture_batch")


def _columns(connection, table: str) -> set[str]:
    return {col["name"] for col in inspect(connection).get_columns(table)}


def upgrade() -> None:
    connection = op.get_bind()
    existing = _columns(connection, _CAPTURE)

    if "content_type" not in existing:
        op.add_column(_CAPTURE, sa.Column("content_type", sa.Text(), nullable=True))
    if "body_sha256" not in existing:
        op.add_column(_CAPTURE, sa.Column("body_sha256", sa.Text(), nullable=True))
    if "body_truncated" not in existing:
        op.add_column(
            _CAPTURE,
            sa.Column(
                "body_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
        )
    if "capture_batch" not in existing:
        op.add_column(
            _CAPTURE,
            sa.Column(
                "capture_batch", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
        )

    if "verified_urls" not in _columns(connection, _RUN_SOURCES):
        op.add_column(_RUN_SOURCES, sa.Column("verified_urls", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()

    existing = _columns(connection, _RUN_SOURCES)
    if "verified_urls" in existing:
        with op.batch_alter_table(_RUN_SOURCES) as batch:
            batch.drop_column("verified_urls")

    existing = _columns(connection, _CAPTURE)
    for column in _CAPTURE_COLUMNS:
        if column in existing:
            with op.batch_alter_table(_CAPTURE) as batch:
                batch.drop_column(column)
