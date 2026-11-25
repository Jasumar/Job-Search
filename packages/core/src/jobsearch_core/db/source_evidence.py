"""Verified-URL evidence per run source (plan-scraping.md §Recipe closure R1).

The drift baseline is a set of posting URLs, not a count: run N is compared to
run N-1 by which URLs appeared and which disappeared, so a page that swaps one
posting for another does not read as "no change" (counts are derived, never the
comparison key). ``sources.last_verified_count`` keeps the latest count for
display; ``run_sources.verified_urls`` keeps the set that count came from.

Stored form is a JSON array of unique URLs, sorted, so equal sets encode to
equal strings and a diff never depends on arrival order.

These functions write through the caller's ``Session`` and flush; the caller
commits (same contract as ``db/recipe_state.py``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobsearch_core.db.models import RunSource, Source


@dataclass(frozen=True)
class SourceDrift:
    """Run N's verified URL set against run N-1's, both from ``run_sources``."""

    run_source_id: int
    previous_run_source_id: int
    current_count: int
    previous_count: int
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.added or self.removed)


def normalize_urls(urls: Iterable[str]) -> tuple[str, ...]:
    """Unique, sorted, non-empty URLs.

    Rejects an empty or non-string entry instead of dropping it: a silently
    discarded URL would look like a disappeared posting in the next diff.
    """
    normalized: set[str] = set()
    for url in urls:
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"verified URL must be a non-empty string, got {url!r}")
        normalized.add(url)
    return tuple(sorted(normalized))


def encode_urls(urls: Iterable[str]) -> str:
    return json.dumps(list(normalize_urls(urls)), ensure_ascii=False)


def decode_urls(payload: str | None) -> frozenset[str]:
    """The stored set; empty for a run that stored nothing (no evidence)."""
    if payload is None:
        return frozenset()
    data = json.loads(payload)
    if not isinstance(data, list) or not all(isinstance(url, str) for url in data):
        raise ValueError(f"verified_urls is not a JSON array of strings: {payload!r}")
    return frozenset(data)


def record_verified_urls(session: Session, run_source_id: int, urls: Iterable[str]) -> int:
    """Store this run's verified URL set; return its size.

    A row without a ``source_id`` (a ``careers_search`` row) has no saved source
    to update, so only ``run_sources.verified_urls`` is written. A missing row
    writes nothing and returns 0.
    """
    row = session.get(RunSource, run_source_id)
    if row is None:
        return 0
    normalized = normalize_urls(urls)
    row.verified_urls = json.dumps(list(normalized), ensure_ascii=False)
    if row.source_id is not None:
        source = session.get(Source, row.source_id)
        if source is not None:
            source.last_verified_count = len(normalized)
            source.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()
    return len(normalized)


def clear_verified_urls(session: Session, run_source_id: int) -> None:
    """Withdraw this run's URL set so it is never the next run's drift baseline.

    For a replay the posting gate judged junk: its URLs were recorded before the
    verdict, and a junk set as baseline would flag the next good run as drift.
    ``previous_run_source`` skips rows with no URL evidence.
    """
    row = session.get(RunSource, run_source_id)
    if row is None:
        return
    row.verified_urls = None
    session.flush()


def baseline_url_count(session: Session, run_source_id: int) -> int:
    """Size of the previous run's verified URL set for this owner; 0 without one."""
    row = session.get(RunSource, run_source_id)
    if row is None:
        return 0
    previous = previous_run_source(session, row)
    if previous is None:
        return 0
    return len(decode_urls(previous.verified_urls))


def previous_run_source(session: Session, row: RunSource) -> RunSource | None:
    """The most recent earlier run with URL evidence for the same owner.

    Same ``source_id`` for a ``name_collect`` row, same ``company_id`` for a
    ``careers_search`` row. Runs that stored no URL set are skipped: a crashed
    or unattempted run is not a baseline.
    """
    statement = select(RunSource).where(
        RunSource.id < row.id, RunSource.verified_urls.is_not(None)
    )
    if row.source_id is not None:
        statement = statement.where(RunSource.source_id == row.source_id)
    elif row.company_id is not None:
        statement = statement.where(RunSource.company_id == row.company_id)
    else:
        return None
    return session.scalars(statement.order_by(RunSource.id.desc()).limit(1)).first()


def compare_to_previous(session: Session, run_source_id: int) -> SourceDrift | None:
    """Drift of this run against the previous one, or None without a baseline."""
    row = session.get(RunSource, run_source_id)
    if row is None or row.verified_urls is None:
        return None
    previous = previous_run_source(session, row)
    if previous is None:
        return None
    current = decode_urls(row.verified_urls)
    before = decode_urls(previous.verified_urls)
    return SourceDrift(
        run_source_id=row.id,
        previous_run_source_id=previous.id,
        current_count=len(current),
        previous_count=len(before),
        added=tuple(sorted(current - before)),
        removed=tuple(sorted(before - current)),
    )
