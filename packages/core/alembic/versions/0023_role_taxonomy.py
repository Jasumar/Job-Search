"""Role taxonomy (MF3/MF4): custom roles, the Matches hide reason, new role ids.

- ``user_profile.custom_roles``: JSON list of ``{"text", "ids"}``, the roles the
  owner typed with the taxonomy ids each maps to.
- ``jobs.fit_hidden_reason``: why Matches hides a job by default (its role is
  outside the target roles). NULL = shown.
- ``user_profile.role_families`` moves from the four tech.yaml chip ids to
  ``config/role_taxonomy.yaml`` ids. Unknown values are kept as they are.

Revision ID: 0023_role_taxonomy
Revises: 0022_run_source_dropped
Create Date: 2026-11-24
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0023_role_taxonomy"
down_revision: str | None = "0022_run_source_dropped"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen copy: a migration must not change when the live mapping changes.
_OLD_TO_NEW: dict[str, tuple[str, ...]] = {
    "backend": ("technology/backend",),
    "data": ("technology/data-engineer", "technology/data-analyst"),
    "ai": ("technology/ml-engineer",),
    "automation": ("technology/devops",),
}
_NEW_TO_OLD: dict[str, str] = {new: old for old, news in _OLD_TO_NEW.items() for new in news}


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _remap(raw: str | None, mapping) -> str:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return raw or "[]"
    if not isinstance(values, list):
        return raw or "[]"
    out: list[str] = []
    for value in values:
        for mapped in mapping(str(value)):
            if mapped not in out:
                out.append(mapped)
    return json.dumps(out, ensure_ascii=False)


def _rewrite_role_families(mapping) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, role_families FROM user_profile")).fetchall()
    for row_id, raw in rows:
        bind.execute(
            sa.text("UPDATE user_profile SET role_families = :value WHERE id = :id"),
            {"value": _remap(raw, mapping), "id": row_id},
        )


def upgrade() -> None:
    if "custom_roles" not in _columns("user_profile"):
        op.add_column(
            "user_profile",
            sa.Column("custom_roles", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        )
    if "fit_hidden_reason" not in _columns("jobs"):
        op.add_column("jobs", sa.Column("fit_hidden_reason", sa.Text(), nullable=True))
    _rewrite_role_families(lambda value: _OLD_TO_NEW.get(value, (value,)))


def downgrade() -> None:
    _rewrite_role_families(lambda value: (_NEW_TO_OLD.get(value, value),))
    if "fit_hidden_reason" in _columns("jobs"):
        op.drop_column("jobs", "fit_hidden_reason")
    if "custom_roles" in _columns("user_profile"):
        op.drop_column("user_profile", "custom_roles")
