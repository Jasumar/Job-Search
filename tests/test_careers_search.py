"""Paced, cached careers search (plan-source-runs §Locked decisions)."""

from __future__ import annotations

import json
import random
from datetime import datetime

import pytest
from jobsearch_core.db.models import Company, SearchCache
from jobsearch_core.search.careers_search import (
    FLOOR_SECONDS,
    MAX_CEILING_SECONDS,
    PAID_CEILING_SECONDS,
    PAID_FLOOR_SECONDS,
    START_CEILING_SECONDS,
    AdaptivePacer,
    PacedSearchClient,
    build_careers_query,
    cached_search,
)
from jobsearch_core.search.client import SearchResult


def _pacer(clock_values=None, rng_seed=0):
    slept: list[float] = []
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = AdaptivePacer(clock=clock, sleep=sleep, rng=random.Random(rng_seed))
    return pacer, slept, now


def test_build_query_uses_suffix_when_present_and_skips_when_absent():
    assert build_careers_query("Acme", "Oy") == "Acme Oy careers open positions"
    assert build_careers_query("Acme") == "Acme careers open positions"


def test_first_wait_does_not_sleep_and_later_waits_stay_in_range():
    pacer, slept, now = _pacer()
    assert pacer.wait() == 0.0
    for _ in range(50):
        pacer.wait()
    assert slept, "consecutive searches must be spaced"
    assert all(FLOOR_SECONDS - 1e-9 <= s <= START_CEILING_SECONDS + 1e-9 for s in slept)


def test_wait_counts_time_already_elapsed_against_the_delay():
    pacer, slept, now = _pacer()
    pacer.wait()
    now[0] += 1000.0  # plenty of real time passed; no sleep is needed
    assert pacer.wait() == 0.0


def test_block_lengthens_ceiling_and_is_capped():
    pacer, _, _ = _pacer()
    pacer.on_block()
    assert pacer.ceiling > START_CEILING_SECONDS
    for _ in range(50):
        pacer.on_block()
    assert pacer.ceiling == MAX_CEILING_SECONDS


def test_clean_streak_shortens_but_never_below_start_ceiling():
    pacer, _, _ = _pacer()
    pacer.on_block()
    widened = pacer.ceiling
    for _ in range(10):
        pacer.on_clean()
    assert pacer.ceiling < widened
    for _ in range(500):
        pacer.on_clean()
    assert pacer.ceiling == START_CEILING_SECONDS


def test_block_resets_clean_streak():
    pacer, _, _ = _pacer()
    pacer.on_block()
    widened = pacer.ceiling
    for _ in range(9):
        pacer.on_clean()
    pacer.on_block()
    pacer.on_clean()  # streak restarted, so no shortening yet
    assert pacer.ceiling > widened


def test_paid_backend_pacer_stays_at_one_request_per_second_or_slower():
    pacer = AdaptivePacer.for_backend("brave")
    assert pacer.floor >= 1.0
    assert pacer.ceiling >= pacer.floor
    assert pacer.ceiling <= PAID_CEILING_SECONDS


def test_free_backend_pacer_keeps_the_human_scale_ladder():
    pacer = AdaptivePacer.for_backend("ddgs")
    assert pacer.floor == FLOOR_SECONDS
    assert pacer.ceiling == START_CEILING_SECONDS


class _RecordingClient:
    """Minimal SearchClient: records queries so the wrapper's order is visible."""

    def __init__(self):
        self.queries: list[str] = []
        self.closed = False

    def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        return []

    def close(self) -> None:
        self.closed = True


def test_paced_client_waits_before_every_search_and_closes_the_inner_client():
    inner = _RecordingClient()
    pacer, slept, _ = _pacer()
    client = PacedSearchClient(inner, pacer)

    client.search("first")
    client.search("second")
    client.search("third")

    assert inner.queries == ["first", "second", "third"]
    # The first wait is free; every later search is spaced.
    assert len(slept) == 2
    client.close()
    assert inner.closed


def test_cached_search_searches_once_then_serves_cache(session):
    company = Company(name="Acme")
    session.add(company)
    session.commit()
    calls: list[str] = []

    def search(query: str) -> list[SearchResult]:
        calls.append(query)
        return [SearchResult(url="https://acme.example/careers", title="Careers", snippet="Jobs")]

    pacer, slept, _ = _pacer()
    query = build_careers_query("Acme")
    first = cached_search(session, company_id=company.id, query=query, search=search, pacer=pacer)
    second = cached_search(session, company_id=company.id, query=query, search=search, pacer=pacer)

    assert calls == [query]
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.results == first.results
    assert len(slept) == 0  # one search only, so no pacing sleep


def test_cached_search_paces_between_real_searches(session):
    company = Company(name="Beta")
    session.add(company)
    session.commit()
    pacer, slept, _ = _pacer()
    for name in ("q1", "q2", "q3"):
        cached_search(session, company_id=company.id, query=name, search=lambda q: [], pacer=pacer)
    assert len(slept) == 2
    assert all(FLOOR_SECONDS - 1e-9 <= s <= START_CEILING_SECONDS + 1e-9 for s in slept)


def test_cached_search_misses_a_row_written_by_another_backend(session):
    """A paid backend must not replay the free engines' blocked-run misses."""
    company = Company(name="Gamma")
    session.add(company)
    session.commit()
    query = build_careers_query("Gamma")
    ddgs_results = [SearchResult(url="https://gamma.example/careers", title="Careers")]
    brave_results = [SearchResult(url="https://gamma.example/jobs", title="Jobs")]

    cached_search(
        session,
        company_id=company.id,
        query=query,
        search=lambda q: ddgs_results,
        pacer=_pacer()[0],
        backend="ddgs",
    )
    found = cached_search(
        session,
        company_id=company.id,
        query=query,
        search=lambda q: brave_results,
        pacer=_pacer()[0],
        backend="brave",
    )

    assert found.from_cache is False
    assert found.results == brave_results

    # The second brave read is a hit, and it replaced the ddgs payload.
    again = cached_search(
        session,
        company_id=company.id,
        query=query,
        search=lambda q: pytest.fail("must not search twice under one backend"),
        pacer=_pacer()[0],
        backend="brave",
    )
    assert again.from_cache is True
    assert again.results == brave_results


def test_cached_search_treats_a_legacy_bare_list_row_as_a_miss(session):
    """Rows written before the envelope carry no backend and must not be trusted."""
    company = Company(name="Delta")
    session.add(company)
    session.commit()
    query = build_careers_query("Delta")
    session.add(
        SearchCache(
            company_id=company.id,
            query=query,
            payload=json.dumps([{"url": "https://delta.example/old", "title": "old", "snippet": ""}]),
            fetched_at=datetime(2026, 11, 1),
        )
    )
    session.commit()

    found = cached_search(
        session,
        company_id=company.id,
        query=query,
        search=lambda q: [SearchResult(url="https://delta.example/new", title="new")],
        pacer=_pacer()[0],
        backend="brave",
    )
    assert found.from_cache is False
    assert [r.url for r in found.results] == ["https://delta.example/new"]


def test_cached_search_without_a_backend_still_serves_any_row(session):
    company = Company(name="Epsilon")
    session.add(company)
    session.commit()
    query = build_careers_query("Epsilon")
    results = [SearchResult(url="https://eps.example/careers", title="Careers")]

    first = cached_search(
        session, company_id=company.id, query=query, search=lambda q: results, pacer=_pacer()[0]
    )
    second = cached_search(
        session, company_id=company.id, query=query, search=lambda q: results, pacer=_pacer()[0]
    )
    assert first.from_cache is False
    assert second.from_cache is True


def test_pacer_for_a_paid_backend_is_a_local_rate_cap_not_a_politeness_delay():
    paid = AdaptivePacer.for_backend("brave")
    assert (paid.floor, paid.ceiling) == (PAID_FLOOR_SECONDS, PAID_CEILING_SECONDS)

    free = AdaptivePacer.for_backend("ddgs")
    assert (free.floor, free.ceiling) == (FLOOR_SECONDS, START_CEILING_SECONDS)
