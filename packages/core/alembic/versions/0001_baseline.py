"""${message}

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-11

Baseline revision: the schema state produced by ``Base.metadata.create_all``
plus the retired ad-hoc startup upgrades (Phases up to 3.6d/M1 worker-basics).
No schema operations — new databases are created by create_all and stamped
here; existing databases already carry this schema.
"""
from __future__ import annotations

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Nothing to do: this revision only exists as the stamp point.
    # Guard for a legacy database missing tables create_all assumes: create_all
    # runs before migrations in init_db, so all model tables exist already.
    op.get_bind()


def downgrade() -> None:
    pass
