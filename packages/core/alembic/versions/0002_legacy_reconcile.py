"""Legacy schema reconciliation (Phase 3.6a/3.6 upgrades, now as Alembic).

The ad-hoc startup upgrades in ``db.session`` are retired (Phase 3.8f
pre-task, decided 2026-09-11); this migration preserves their behavior for
databases that never ran them: rename ``companies.tech_employer`` to
``employer_domain`` (values preserved) and copy any legacy
``discovery_queue`` rows into ``companies`` before dropping the old table.

Both steps are idempotent no-ops on databases that are already current.

Revision ID: 0002_legacy_reconcile
Revises: 0001_baseline
Create Date: 2026-09-11
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

revision: str = "0002_legacy_reconcile"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def _rename_tech_employer_column(connection) -> None:
    """Rename ``companies.tech_employer`` to ``employer_domain`` (Phase 3.6a)."""
    inspector = inspect(connection)
    if "companies" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("companies")}
    if "tech_employer" not in columns or "employer_domain" in columns:
        return
    connection.execute(text('ALTER TABLE "companies" RENAME COLUMN "tech_employer" TO "employer_domain"'))


def _migrate_discovery_queue(connection) -> None:
    """Copy legacy ``discovery_queue`` rows into ``companies`` and drop the old table.

    Rows are copied verbatim by shared column name, preserving each row's
    current status (nothing is auto-promoted to ``known``). The legacy table is
    dropped only after the copy, so the migration is idempotent.
    """
    inspector = inspect(connection)
    if "discovery_queue" not in inspector.get_table_names() or "companies" not in inspector.get_table_names():
        return
    target_cols = {column["name"]: column for column in inspector.get_columns("companies")}
    rows = connection.execute(text('SELECT * FROM "discovery_queue"')).mappings().all()
    old_names = {key for row in rows for key in row.keys()} if rows else set()
    new_names = set(target_cols)
    shared = sorted(new_names & old_names)
    # Phase 3.6a renamed the identity classification column: a legacy
    # `tech_employer` source column is copied into `employer_domain`.
    source_to_target = {name: name for name in shared}
    if "tech_employer" in old_names and "employer_domain" in new_names:
        source_to_target["tech_employer"] = "employer_domain"
    for row in rows:
        # Copy shared columns, but skip NULL values whose target column is NOT
        # NULL with a server default so the target default fills them (e.g. a
        # legacy row may hold NULL for discovered_at). This keeps the copy
        # valid even against an older or hand-built legacy table.
        cols: list[str] = []
        values: dict[str, object] = {}
        for source_name in source_to_target:
            val = row[source_name]
            target_name = source_to_target[source_name]
            target = target_cols.get(target_name)
            if val is None and target and not target["nullable"] and target.get("default"):
                continue
            cols.append(target_name)
            values[target_name] = val
        if not cols:
            continue
        columns = ", ".join(f'"{name}"' for name in cols)
        placeholders = ", ".join(f":{name}" for name in cols)
        connection.execute(
            text(f'INSERT INTO "companies" ({columns}) VALUES ({placeholders})'), values
        )
    connection.execute(text('DROP TABLE "discovery_queue"'))


# The columns the retired ad-hoc startup upgrade added (Phases 3.6–3.6d),
# preserved verbatim so legacy databases reach the same state.
_ALIGNMENT_COLUMNS = {
    "jobs": {
        "authority_url": "TEXT",
        "extraction_url": "TEXT",
        "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
        "verified_at": "DATETIME",
        "closing_date": "TEXT",
        "remote_policy": "TEXT",
        "employment_type": "TEXT",
        "seniority": "TEXT",
        "languages_required": "TEXT",
        "work_authorization": "TEXT",
        "sponsorship": "TEXT",
        "salary": "TEXT",
        "extracted_requirements": "TEXT",
        "job_profile": "TEXT",
        "link_checked_at": "DATETIME",
        "link_ok": "BOOLEAN",
        "fit_gaps": "TEXT",
        "fit_strengths": "TEXT",
        "fit_blockers": "TEXT",
        "fit_summary": "TEXT",
        "included_families": "TEXT",
        "include_signals": "TEXT",
    },
    "companies": {
        "notes": "TEXT NOT NULL DEFAULT ''",
        "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
        "verified_at": "DATETIME",
        "source_url": "TEXT",
        "resolution_status": "TEXT NOT NULL DEFAULT 'unresolved'",
        "resolution_page_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "triage_decision": "TEXT",
        "triage_source_type": "TEXT",
        "triage_confidence": "REAL",
        "triage_evidence": "TEXT",
        "triage_reason": "TEXT",
        "identity_name": "TEXT",
        "identity_confidence": "REAL",
        "identity_evidence": "TEXT",
        "identity_reason": "TEXT",
        "employer_domain": "TEXT",
        "followup_query": "TEXT",
    },
}


def _add_alignment_columns(connection) -> None:
    """Add the Phase 3.6-era alignment columns missing on existing tables."""
    inspector = inspect(connection)
    for table, columns in _ALIGNMENT_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'))


def upgrade() -> None:
    connection = op.get_bind()
    _rename_tech_employer_column(connection)
    _add_alignment_columns(connection)
    _migrate_discovery_queue(connection)


def downgrade() -> None:
    # Irreversible by design: the legacy table/column names are not restored.
    pass
