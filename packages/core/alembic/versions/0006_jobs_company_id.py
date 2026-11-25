"""Add jobs.company_id: the job-to-company foreign key (gap 12).

Until now the only link from a job to the company it was scraped from was a
text join: jobs.source_url vs companies.careers_url, plus normalized_company.

The column is nullable on purpose. A job scraped from a bare URL has no
company row behind it, and existing rows are only backfilled when provably
resolvable, so a wrong guess is impossible by construction.

Revision ID: 0006_jobs_company_id
Revises: 0005_user_evidence
Create Date: 2026-10-02
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0006_jobs_company_id"
down_revision: str | None = "0005_user_evidence"
branch_labels: str | None = None
depends_on: str | None = None


def _canon_url(value: str | None) -> str:
    """Canonicalize a careers URL. Mirrors company_link.canonical_url.

    Duplicated from jobsearch_core.db.company_link on purpose: a migration
    must not import app code that may later be refactored away. The two must
    stay in step, or the backfill attributes differently than live ingest does.
    """
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def _canon_name(value: str | None) -> str:
    """Canonicalize a company name. Mirrors company_link.canonical_name.

    Strips trailing commas/periods the way the job normalizer does, so that
    "Ambiguous Co" and "Ambiguous Co." collide here exactly as they do at
    ingest time and are correctly treated as ambiguous.
    """
    if not value:
        return ""
    text = re.sub(r"\s+", " ", value.strip()).lower()
    return re.sub(r"[,.]$", "", text)


def _backfill(connection) -> tuple[int, int]:
    """Fill company_id for existing rows by exact URL, then exact name.

    Returns (linked, skipped_ambiguous). Rows are only linked on an exact
    canonical match, and a canonical name owned by two companies is left NULL
    rather than guessed.
    """
    jobs = connection.execute(
        sa.text("SELECT id, source_url, normalized_company FROM jobs")
    ).fetchall()

    by_url: dict[str, int] = {}
    by_name: dict[str, int] = {}
    ambiguous: set[str] = set()
    for cid, careers_url, name in connection.execute(
        sa.text("SELECT id, careers_url, name FROM companies")
    ).fetchall():
        key = _canon_url(careers_url)
        if key:
            by_url.setdefault(key, cid)
        nkey = _canon_name(name)
        if nkey:
            if nkey in by_name and by_name[nkey] != cid:
                ambiguous.add(nkey)
            else:
                by_name[nkey] = cid
    for key in ambiguous:
        by_name.pop(key, None)

    linked = 0
    for jid, source_url, norm_company in jobs:
        cid = by_url.get(_canon_url(source_url))
        if cid is None:
            nkey = _canon_name(norm_company)
            cid = by_name.get(nkey) if nkey else None
        if cid is None:
            continue
        connection.execute(
            sa.text("UPDATE jobs SET company_id = :cid WHERE id = :jid"),
            {"cid": cid, "jid": jid},
        )
        linked += 1
    return linked, len(ambiguous)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)

    columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "company_id" not in columns:
        op.add_column(
            "jobs",
            sa.Column("company_id", sa.Integer(), nullable=True),
        )
        op.create_index("ix_jobs_company_id", "jobs", ["company_id"])

    if "ix_companies_name" not in {
        i["name"] for i in inspector.get_indexes("companies")
    }:
        op.create_index("ix_companies_name", "companies", ["name"])

    linked, ambiguous = _backfill(connection)
    print(f"gap 12 backfill: linked {linked} jobs, skipped {ambiguous} ambiguous names")


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "company_id" in columns:
        op.drop_index("ix_jobs_company_id", table_name="jobs")
        op.drop_column("jobs", "company_id")
    if "ix_companies_name" in {i["name"] for i in inspector.get_indexes("companies")}:
        op.drop_index("ix_companies_name", table_name="companies")
