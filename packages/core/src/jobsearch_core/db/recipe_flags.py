"""Storage for posting-existence gate answers (R3, plan-scraping.md §Recipe closure R3).

A flag is keyed by the recipe body hash, never by the recipe row: the analysis
explains one exact body, and a re-authored body must not inherit it. Rows are a
history (one per gate event), so the stop rule can ask whether this body has
already been answered "none" once.

Writes nothing but ``recipe_flags`` rows, and flushes; the caller commits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobsearch_core.db.models import RecipeFlag

VERDICTS = ("none", "postings")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _check_body_sha256(body_sha256: str) -> str:
    """Refuse anything that is not a body hash: two bodies must not share a key."""
    if not _SHA256.match(body_sha256):
        raise ValueError(f"body_sha256 must be 64 lowercase hex characters: {body_sha256!r}")
    return body_sha256


def record_flag(
    session: Session,
    *,
    body_sha256: str,
    verdict: str,
    analysis: str | dict | list,
    stopped: bool = False,
    model_id: str | None = None,
    page_url: str | None = None,
    recipe_id: int | None = None,
) -> RecipeFlag:
    """Store one gate answer. ``analysis`` is the gate's per-item reasoning."""
    _check_body_sha256(body_sha256)
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}: {verdict!r}")
    payload = analysis if isinstance(analysis, str) else json.dumps(analysis, ensure_ascii=False)
    row = RecipeFlag(
        recipe_id=recipe_id,
        body_sha256=body_sha256,
        page_url=page_url,
        verdict=verdict,
        model_id=model_id,
        analysis=payload,
        stopped=stopped,
    )
    session.add(row)
    session.flush()
    return row


def flags_for_body(session: Session, body_sha256: str) -> list[RecipeFlag]:
    """Every flag recorded for this exact body, oldest first."""
    _check_body_sha256(body_sha256)
    return list(
        session.scalars(
            select(RecipeFlag)
            .where(RecipeFlag.body_sha256 == body_sha256)
            .order_by(RecipeFlag.created_at, RecipeFlag.id)
        )
    )


def latest_flag(session: Session, body_sha256: str) -> RecipeFlag | None:
    rows = flags_for_body(session, body_sha256)
    return rows[-1] if rows else None


def has_prior_none(session: Session, body_sha256: str) -> bool:
    """Whether this body has already been answered "none" (the stop agreement)."""
    return any(flag.verdict == "none" for flag in flags_for_body(session, body_sha256))


def stopped_bodies(session: Session) -> Iterable[str]:
    """Body hashes that carry a stop, for callers that gate before replaying."""
    return session.scalars(select(RecipeFlag.body_sha256).where(RecipeFlag.stopped.is_(True))).all()
