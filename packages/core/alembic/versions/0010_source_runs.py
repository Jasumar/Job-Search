"""Create run_sources, sources, search_cache: tables for M1 source-driven runs.

Shapes follow docs/plan-source-runs.md §Model.

- ``sources``: listing-page URLs saved from ``name_collect`` runs. Saved sources
  have their own lifecycle; nothing here references ``job_runs``, so pruning a
  run never removes a saved source.
- ``run_sources``: per-item progress rows for a run. A ``name_collect`` row owns
  a ``source_id``; a ``careers_search`` row owns a ``company_id``. The CHECK
  constraint keeps each row to exactly one owner.
- ``search_cache``: one cached search result per (company, query), so a rerun
  does not search again.

``job_runs.status`` gaining ``paused`` needs no migration: it is plain text with
no constraint.

Revision ID: 0010_source_runs
Revises: 0009_company_recipes
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0010_source_runs"
down_revision: str | None = "0009_company_recipes"
branch_labels: str | None = None
depends_on: str | None = None

_SOURCES = "sources"
_RUN_SOURCES = "run_sources"
_SEARCH_CACHE = "search_cache"
_TABLES = (_RUN_SOURCES, _SOURCES, _SEARCH_CACHE)


def upgrade() -> None:
    connection = op.get_bind()
    existing = set(inspect(connection).get_table_names())

    if _SOURCES not in existing:
        op.create_table(
            _SOURCES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("url", sa.Text(), nullable=False, unique=True),
            sa.Column(
                "recipe_id",
                sa.Integer(),
                sa.ForeignKey("company_recipes.id"),
                nullable=True,
            ),
            sa.Column("last_verified_count", sa.Integer(), nullable=True),
            sa.Column("replay_check", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if _RUN_SOURCES not in existing:
        op.create_table(
            _RUN_SOURCES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("job_runs.id"),
                nullable=False,
            ),
            sa.Column(
                "source_id",
                sa.Integer(),
                sa.ForeignKey("sources.id"),
                nullable=True,
            ),
            sa.Column(
                "company_id",
                sa.Integer(),
                sa.ForeignKey("companies.id"),
                nullable=True,
            ),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("identities_found", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("candidates_added", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("candidates_duplicate", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("extraction", sa.Text(), nullable=True),
            sa.Column(
                "recipe_id",
                sa.Integer(),
                sa.ForeignKey("company_recipes.id"),
                nullable=True,
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ('queued', 'running', 'paused', 'completed',"
                " 'zero_yield', 'failed', 'cancelled')",
                name="ck_run_sources_status",
            ),
            sa.CheckConstraint(
                "extraction IS NULL OR extraction IN ('recipe', 'llm')",
                name="ck_run_sources_extraction",
            ),
            sa.CheckConstraint(
                "(source_id IS NOT NULL AND company_id IS NULL)"
                " OR (source_id IS NULL AND company_id IS NOT NULL)",
                name="ck_run_sources_owner",
            ),
        )
        op.create_index("ix_run_sources_run_id", _RUN_SOURCES, ["run_id"])

    if _SEARCH_CACHE not in existing:
        op.create_table(
            _SEARCH_CACHE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "company_id",
                sa.Integer(),
                sa.ForeignKey("companies.id"),
                nullable=False,
            ),
            sa.Column("query", sa.Text(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "query", name="uq_search_cache_company_query"),
        )


def downgrade() -> None:
    connection = op.get_bind()
    existing = set(inspect(connection).get_table_names())
    if _SEARCH_CACHE in existing:
        op.drop_table(_SEARCH_CACHE)
    if _RUN_SOURCES in existing:
        op.drop_index("ix_run_sources_run_id", table_name=_RUN_SOURCES)
        op.drop_table(_RUN_SOURCES)
    if _SOURCES in existing:
        op.drop_table(_SOURCES)
