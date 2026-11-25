"""When a company row is already a usable hiring source.

One definition shared by the three places that ask this question: the
no-careers selection list (``list_companies``), the careers-search enqueue
validation (``enqueue_careers_search``) and the per-company skip rule inside a
run (``run_careers_search``). They must agree, or the UI offers a row the
worker then refuses to search, or the worker re-searches a row the owner
already decided.

A row is satisfied when it has a durable careers page (``careers_url``), or
when the review gate decided it over a resolved entry (``open_positions_url``)
— the entry is the page the scrape actually uses, so re-resolving it would undo
a human verdict.

``pending`` rows are never satisfied by an entry: that is the correction path.
Sending a row back to ``pending`` makes it re-searchable without any special
endpoint, which is how a stale or wrong entry gets repaired.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_

from jobsearch_core.db.models import Company

#: Statuses whose resolved entry counts as a decided hiring source.
DECIDED_STATUSES: tuple[str, ...] = ("accepted", "known")


def _present(column: Any) -> Any:
    """Column is set and not the empty string older writers left behind."""
    return and_(column.is_not(None), column != "")


def has_source_clause() -> Any:
    """SQL predicate equivalent to :func:`has_source`, for query filters."""
    return or_(
        _present(Company.careers_url),
        and_(
            Company.status.in_(DECIDED_STATUSES),
            _present(Company.open_positions_url),
        ),
    )


def has_source(company: Company) -> bool:
    """True when this row already has a hiring source the scrape can use."""
    if (company.careers_url or "").strip():
        return True
    return company.status in DECIDED_STATUSES and bool(
        (company.open_positions_url or "").strip()
    )
