"""Paced, cached company careers search for the ``careers_search`` run type.

Plan: docs/plan-source-runs.md §Locked decisions (search pacing, search cache).

- ``AdaptivePacer`` spaces searches by a random 5-15 s delay (owner-approved
  starting range). A block or throttle signal lengthens the range; a clean
  streak shortens it slowly, never below the floor.
- ``cached_search`` returns a stored result for (company, query) before
  searching, so a rerun does not search again.

The pacer takes injectable ``clock``/``sleep``/``rng`` so tests run instantly.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from jobsearch_core.db.models import SearchCache
from jobsearch_core.search.client import SearchClient, SearchResult

FLOOR_SECONDS = 5.0
START_CEILING_SECONDS = 15.0
MAX_CEILING_SECONDS = 120.0
CLEAN_STREAK_TO_SHORTEN = 10
_LENGTHEN_FACTOR = 1.5
_SHORTEN_STEP = 0.9

#: Backends that are not throttled per IP. Their spacing is a request-rate cap,
#: not a politeness delay: Brave's free plan answers 1 request/second (the paid
#: plan 50), so the floor stays at one second and at least a second of jitter
#: on top. A 75-request identity-first run spends ~90 s asleep, which is the
#: price of never spending a retry on a 429.
PAID_SEARCH_BACKENDS = frozenset({"brave"})
PAID_FLOOR_SECONDS = 1.0
PAID_CEILING_SECONDS = 2.0


def build_careers_query(name: str, suffix: str | None = None) -> str:
    """``name + suffix + careers/open positions``. Suffix is config, not code."""
    parts = [name.strip()]
    if suffix:
        parts.append(suffix.strip())
    parts.append("careers open positions")
    return " ".join(parts)


@dataclass
class AdaptivePacer:
    """Random delay between searches; widens on block, narrows after clean streak."""

    floor: float = FLOOR_SECONDS
    ceiling: float = START_CEILING_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = None  # type: ignore[assignment]
    _last: float | None = None
    _clean_streak: int = 0

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random()

    @classmethod
    def for_backend(cls, backend: str) -> AdaptivePacer:
        """A pacer sized for *backend*.

        A free engine is protected by human-scale spacing; a paid backend only
        needs to stay under its request rate, so the same ladder would make a
        run sleep for nothing.
        """
        if backend in PAID_SEARCH_BACKENDS:
            return cls(floor=PAID_FLOOR_SECONDS, ceiling=PAID_CEILING_SECONDS)
        return cls()

    def wait(self) -> float:
        """Sleep the remainder of a random delay since the previous search. Returns seconds slept."""
        delay = self.rng.uniform(self.floor, self.ceiling)
        slept = 0.0
        if self._last is not None:
            remaining = delay - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
                slept = remaining
        self._last = self.clock()
        return slept

    def on_clean(self) -> None:
        self._clean_streak += 1
        if self._clean_streak >= CLEAN_STREAK_TO_SHORTEN and self.ceiling > START_CEILING_SECONDS:
            self.ceiling = max(START_CEILING_SECONDS, self.ceiling * _SHORTEN_STEP)
            self._clean_streak = 0

    def on_block(self) -> None:
        self._clean_streak = 0
        self.ceiling = min(MAX_CEILING_SECONDS, self.ceiling * _LENGTHEN_FACTOR)


class PacedSearchClient:
    """Spaces consecutive searches of the wrapped client.

    Discovery issued every baseline and follow-up query back to back; the pacer
    existed only inside ``careers_search``. A free engine answered that burst
    with challenge pages and a paid one with 429s. The delay comes from
    :meth:`AdaptivePacer.wait`, so the same spacing rule applies to both.
    """

    def __init__(self, client: SearchClient, pacer: AdaptivePacer):
        self._client = client
        self._pacer = pacer

    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        self._pacer.wait()
        return self._client.search(query, num_results=num_results)

    def close(self) -> None:
        self._client.close()


@dataclass
class CachedSearch:
    results: list[SearchResult]
    from_cache: bool


def _encode(results: list[SearchResult], backend: str | None) -> str:
    """Store the writing backend beside the results.

    A payload written by ddgs (or by the older bare-list format) is not a valid
    answer for a different engine: a row cached while the free engines were
    blocked holds ``[]`` or Yahoo-only leftovers. Keeping the name lets
    ``cached_search`` miss on it instead of replaying a throttled run's misses.
    """
    return json.dumps(
        {"backend": backend, "results": [asdict(r) for r in results]}
    )


def _decode(payload: str) -> tuple[str | None, list[SearchResult]]:
    data = json.loads(payload)
    if isinstance(data, list):  # rows written before the envelope
        return None, [SearchResult(**item) for item in data]
    return data.get("backend"), [SearchResult(**item) for item in data.get("results", [])]


def cached_search(
    session: Session,
    *,
    company_id: int,
    query: str,
    search: Callable[[str], list[SearchResult]],
    pacer: AdaptivePacer,
    backend: str | None = None,
) -> CachedSearch:
    """Return the cached result for (company, query), or pace, search, and store it.

    ``backend`` names the engine that would answer. A row written by another
    backend is not a hit; an empty result is cached (it is a real answer for
    this engine) and served on a rerun.
    """
    row = (
        session.query(SearchCache)
        .filter(SearchCache.company_id == company_id, SearchCache.query == query)
        .first()
    )
    if row is not None:
        stored_backend, results = _decode(row.payload)
        if backend is None or stored_backend == backend:
            return CachedSearch(results=results, from_cache=True)

    pacer.wait()
    results = search(query)
    payload = _encode(results, backend)
    if row is None:
        session.add(
            SearchCache(
                company_id=company_id,
                query=query,
                payload=payload,
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    else:
        row.payload = payload
        row.fetched_at = datetime.now(UTC).replace(tzinfo=None)
    session.commit()
    return CachedSearch(results=results, from_cache=False)
