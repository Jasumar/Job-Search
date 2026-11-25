"""M1 B2: the ``careers_search`` worker executes the manifest.

The properties under test (plan-source-runs.md §B2):

- a hit records the 4.0d entry (``open_positions_url``) and never writes
  ``careers_url`` — the human review gate is its only writer;
- a miss is ``zero_yield`` on the run row and never touches the company;
- the company boundary is the checkpoint: pause and cancel stop there, a
  resumed run skips rows that are already terminal;
- a cache hit spends no search request and no pacing;
- a persistent block pauses the run (resumable) instead of failing it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

import pytest
from jobsearch_core.db.models import Company, JobRun, RunSource, SearchCache
from jobsearch_core.db.session import get_session
from jobsearch_core.ingest.scrapers.entry_resolution import (
    EntryCandidate,
    EntryReport,
    EntryResolution,
    EntryTried,
)
from jobsearch_core.search.careers_search import AdaptivePacer, build_careers_query
from jobsearch_core.search.client import (
    SEARCH_FAILURE_NO_ENGINE,
    SearchError,
    SearchRefusedError,
    SearchResult,
)
from jobsearch_core.source_runs import run_careers_search
from jobsearch_worker.consumer import claim_next_run, enqueue_careers_search, execute_run


class FakeClient:
    """The fetch boundary: one HTML page per URL, or a per-URL exception."""

    def __init__(self, pages: dict[str, str] | None = None, errors: dict[str, Exception] | None = None):
        self.pages = pages or {}
        self.errors = errors or {}
        self.fetched: list[str] = []
        self.closed = False

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if url in self.errors:
            raise self.errors[url]
        return self.pages.get(url, "<html><body>nothing here</body></html>")

    def close(self) -> None:
        self.closed = True


def _pacer() -> AdaptivePacer:
    return AdaptivePacer(sleep=lambda _seconds: None)


def _company(session, name: str, **kwargs) -> Company:
    company = Company(name=name, **kwargs)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


def _started_run(session, companies: list[Company]) -> JobRun:
    run = enqueue_careers_search(session, [company.id for company in companies])
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run.id
    return claimed


def _entry(
    url: str = "https://acme.example/careers/avoimet",
    landing: str = "https://acme.example/careers",
) -> EntryReport:
    """A hit report, as ``resolve_scraping_entry`` returns it."""
    return EntryReport(
        resolution=EntryResolution(
            url=url, landing_url=landing, link_text="Avoimet työpaikat", hops=1, postings=3
        ),
        reason="resolved",
        tried=[EntryTried(url=url, score=5, outcome="postings")],
        candidates=[EntryCandidate(url=url, link_text="Avoimet työpaikat", score=5)],
    )


def _miss(reason: str = "no page under https://acme.example/careers yields postings") -> EntryReport:
    """A miss report: what the row must show instead of only a log line."""
    return EntryReport(
        resolution=None,
        reason=reason,
        tried=[EntryTried(url="https://acme.example/careers/ura", score=2, outcome="no_postings")],
        candidates=[
            EntryCandidate(url="https://acme.example/careers/jobs", link_text="Jobs", score=4),
            EntryCandidate(url="https://acme.example/careers/ura", link_text="Uramme", score=2),
        ],
        sitemap="https://acme.example/sitemap.xml",
    )


def _results(*urls: str) -> list[SearchResult]:
    return [SearchResult(url=url, title="Acme careers", snippet="Avoimet työpaikat") for url in urls]


def _rows(session, run: JobRun) -> list[RunSource]:
    session.expire_all()
    return session.query(RunSource).filter(RunSource.run_id == run.id).order_by(RunSource.id).all()


def test_a_hit_records_the_entry_and_never_careers_url(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    client = FakeClient(pages={"https://acme.example/careers": "<html></html>"})

    summary = run_careers_search(
        session,
        run,
        search=lambda query: _results("https://acme.example/careers"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    session.expire_all()
    stored = session.get(Company, company.id)
    row = _rows(session, run)[0]
    assert stored.open_positions_url == "https://acme.example/careers/avoimet"
    assert stored.open_positions_landing_url == "https://acme.example/careers"
    assert stored.open_positions_link_text == "Avoimet työpaikat"
    # The review endpoint is the only writer of careers_url.
    assert stored.careers_url is None
    assert row.status == "completed"
    assert row.identities_found == 1
    assert row.error is None
    # run_sources.extraction is a constrained flag (recipe/llm), not a blob.
    assert row.extraction is None
    assert summary["pages"] == 1
    assert summary["stopped"] is None


def test_a_hit_writes_the_entry_trace_to_the_row(session, monkeypatch):
    """The evidence rides on the row, hit or miss (Phase 4.0d item 2)."""
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    client = FakeClient(pages={"https://acme.example/careers": "<html></html>"})

    run_careers_search(
        session,
        run,
        search=lambda query: _results("https://acme.example/careers"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.entry_trace is not None
    trace = json.loads(row.entry_trace)
    assert trace["entry"]["url"] == "https://acme.example/careers/avoimet"
    assert trace["tried"][0]["outcome"] == "postings"


def test_a_miss_writes_the_trace_so_the_ui_can_explain_it(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _miss())
    client = FakeClient(pages={"https://acme.example/careers": "<html></html>"})

    run_careers_search(
        session,
        run,
        search=lambda query: _results("https://acme.example/careers"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    trace = json.loads(row.entry_trace)
    assert trace["entry"] is None
    assert trace["sitemap"] == "https://acme.example/sitemap.xml"
    assert [tried["outcome"] for tried in trace["tried"]] == ["no_postings"]
    assert [candidate["score"] for candidate in trace["candidates"]] == [4, 2]


def test_a_second_candidate_page_is_tried_from_the_same_search(session, monkeypatch):
    """Both employer pages come from the one paid search result set."""
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    searches: list[str] = []
    resolved: list[str] = []

    def resolve(url, *args, **kwargs):
        resolved.append(url)
        return _miss() if url == "https://acme.example/careers" else _entry()

    monkeypatch.setattr(source_runs, "resolve_scraping_entry", resolve)
    client = FakeClient(
        pages={
            "https://acme.example/careers": "<html></html>",
            "https://acme.example/careers/ura": "<html></html>",
        }
    )

    def search(query: str) -> list[SearchResult]:
        searches.append(query)
        return _results("https://acme.example/careers", "https://acme.example/careers/ura")

    summary = run_careers_search(
        session, run, search=search, pacer=_pacer(), client=client, vocabulary=object()
    )

    session.expire_all()
    stored = session.get(Company, company.id)
    row = _rows(session, run)[0]
    # One search per company: a second start page never costs another request.
    assert searches == [build_careers_query("Acme Oy")]
    assert resolved == ["https://acme.example/careers", "https://acme.example/careers/ura"]
    assert stored.open_positions_url == "https://acme.example/careers/avoimet"
    assert row.status == "completed"
    assert summary["pages"] == 1

    trace = json.loads(row.entry_trace)
    assert trace["entry"]["url"] == "https://acme.example/careers/avoimet"
    assert [page["outcome"] for page in trace["candidate_pages"]] == ["no_entry", "resolved"]
    assert [page["rank"] for page in trace["candidate_pages"]] == [0, 1]


def test_two_candidate_pages_are_the_most_a_company_tries(session, monkeypatch):
    """The queue is capped: a third employer page is left for the next run."""
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    resolved: list[str] = []

    def resolve(url, *args, **kwargs):
        resolved.append(url)
        return _miss()

    monkeypatch.setattr(source_runs, "resolve_scraping_entry", resolve)
    client = FakeClient()

    run_careers_search(
        session,
        run,
        search=lambda query: _results(
            "https://acme.example/careers",
            "https://acme.example/careers/ura",
            "https://acme.example/careers/join",
        ),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert len(resolved) == 2
    assert row.status == "zero_yield"
    trace = json.loads(row.entry_trace)
    assert [page["url"] for page in trace["candidate_pages"]] == list(resolved)


def test_an_unreachable_candidate_page_does_not_fail_the_row(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    client = FakeClient(
        pages={"https://acme.example/careers/ura": "<html></html>"},
        errors={"https://acme.example/careers": RuntimeError("connection reset")},
    )

    summary = run_careers_search(
        session,
        run,
        search=lambda query: _results(
            "https://acme.example/careers", "https://acme.example/careers/ura"
        ),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "completed"
    assert summary["failures"] == 0
    trace = json.loads(row.entry_trace)
    assert [page["outcome"] for page in trace["candidate_pages"]] == [
        "unreachable",
        "resolved",
    ]


def test_no_readable_candidate_page_at_all_is_a_failed_row(session):
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    client = FakeClient(
        errors={
            "https://acme.example/careers": RuntimeError("connection reset"),
            "https://acme.example/careers/ura": RuntimeError("connection reset"),
        }
    )

    summary = run_careers_search(
        session,
        run,
        search=lambda query: _results(
            "https://acme.example/careers", "https://acme.example/careers/ura"
        ),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "failed"
    assert "connection reset" in row.error
    assert summary["failures"] == 1


def test_an_employer_root_page_is_descended_when_no_hiring_path_exists(session, monkeypatch):
    """The relaxed candidate rule: search only has to find the employer origin."""
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    client = FakeClient(pages={"https://acme.example/": "<html></html>"})

    run_careers_search(
        session,
        run,
        search=lambda query: _results("https://acme.example/"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    session.expire_all()
    stored = session.get(Company, company.id)
    assert _rows(session, run)[0].status == "completed"
    assert stored.open_positions_url == "https://acme.example/careers/avoimet"


def test_a_candidate_that_never_yields_postings_is_a_miss(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _miss())
    client = FakeClient(pages={"https://acme.example/careers": "<html></html>"})

    summary = run_careers_search(
        session,
        run,
        search=lambda query: _results("https://acme.example/careers"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    session.expire_all()
    row = _rows(session, run)[0]
    stored = session.get(Company, company.id)
    assert row.status == "zero_yield"
    assert "https://acme.example/careers" in row.error
    assert stored.open_positions_url is None
    assert stored.careers_url is None
    assert summary["misses"] == 1


def test_an_ats_board_among_the_results_is_never_the_careers_page(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(
        source_runs, "resolve_scraping_entry", lambda *a, **k: pytest.fail("no candidate to descend")
    )
    client = FakeClient()

    run_careers_search(
        session,
        run,
        search=lambda query: _results("https://jobs.lever.co/acme", "https://www.linkedin.com/jobs/acme"),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    assert "ATS board host refused: jobs.lever.co (lever)" in row.error
    assert client.fetched == []


def test_an_aggregator_host_is_refused_before_the_fetch_and_named_on_the_row(session):
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    client = FakeClient()

    summary = run_careers_search(
        session,
        run,
        search=lambda query: _results(
            "https://www.glassdoor.com/Jobs/Acme-Oy-Jobs",
            "https://jobcrawls.com/company/acme-oy/jobs",
            "https://acme.eightfold.ai/careers",
        ),
        pacer=_pacer(),
        client=client,
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    assert row.error.startswith("aggregator host refused: www.glassdoor.com")
    assert "jobcrawls.com" in row.error and "acme.eightfold.ai" in row.error
    assert client.fetched == []
    assert summary["misses"] == 1


def test_no_results_is_a_miss_not_a_run_error(session):
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])

    summary = run_careers_search(
        session,
        run,
        search=lambda query: [],
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
    )

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    assert row.error == "no search results"
    assert summary["misses"] == 1


def test_a_fetch_failure_is_a_failed_row_and_the_run_goes_on(session, monkeypatch):
    from jobsearch_core import source_runs

    broken = _company(session, "Acme Oy")
    healthy = _company(session, "Boltic Oy")
    run = _started_run(session, [broken, healthy])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    client = FakeClient(
        pages={
            "https://acme.example/careers": "<html></html>",
            "https://boltic.example/careers": "<html></html>",
        },
        errors={"https://acme.example/careers": RuntimeError("connection reset")},
    )
    seen: list[str] = []

    def search(query: str) -> list[SearchResult]:
        seen.append(query)
        if query.startswith("Acme"):
            return _results("https://acme.example/careers")
        return _results("https://boltic.example/careers")

    summary = run_careers_search(
        session, run, search=search, pacer=_pacer(), client=client, vocabulary=object()
    )

    rows = _rows(session, run)
    assert [row.status for row in rows] == ["failed", "completed"]
    assert "connection reset" in rows[0].error
    assert summary["failures"] == 1
    assert summary["pages"] == 1


def test_the_cache_spends_no_search_and_no_pacing(session, monkeypatch):
    from jobsearch_core import source_runs

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    query = build_careers_query(company.name)
    session.add(
        SearchCache(
            company_id=company.id,
            query=query,
            # The envelope names the engine that answered; a row for another
            # backend is deliberately not a hit (switch to the paid API).
            payload=json.dumps(
                {
                    "backend": "brave",
                    "results": [asdict(result) for result in _results("https://acme.example/careers")],
                }
            ),
            fetched_at=datetime(2026, 11, 1),
        )
    )
    session.commit()
    waited: list[float] = []

    def pacer() -> AdaptivePacer:
        return AdaptivePacer(sleep=waited.append)

    summary = run_careers_search(
        session,
        run,
        search=lambda query: pytest.fail("a cache hit must not search"),
        pacer=pacer(),
        client=FakeClient(pages={"https://acme.example/careers": "<html></html>"}),
        vocabulary=object(),
        backend="brave",
    )

    assert _rows(session, run)[0].status == "completed"
    assert summary["cached"] == 1
    assert waited == []


def test_a_pause_request_stops_at_the_next_company_boundary(session, monkeypatch):
    from jobsearch_core import source_runs

    first = _company(session, "Acme Oy")
    second = _company(session, "Boltic Oy")
    run = _started_run(session, [first, second])
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    calls: list[str] = []

    def pause_from_elsewhere() -> None:
        other = get_session()
        try:
            other.get(JobRun, run.id).status = "paused"
            other.commit()
        finally:
            other.close()

    def search(query: str) -> list[SearchResult]:
        calls.append(query)
        pause_from_elsewhere()
        return _results("https://acme.example/careers")

    summary = run_careers_search(
        session,
        run,
        search=search,
        pacer=_pacer(),
        client=FakeClient(pages={"https://acme.example/careers": "<html></html>"}),
        vocabulary=object(),
    )

    rows = _rows(session, run)
    assert [row.status for row in rows] == ["completed", "queued"]
    assert calls == [build_careers_query("Acme Oy")]
    assert summary["stopped"] == "paused"
    assert session.get(JobRun, run.id).status == "paused"


def test_a_resumed_run_skips_rows_that_are_already_terminal(session, monkeypatch):
    from jobsearch_core import source_runs

    done = _company(session, "Acme Oy")
    todo = _company(session, "Boltic Oy")
    run = _started_run(session, [done, todo])
    first_row = _rows(session, run)[0]
    first_row.status = "completed"
    first_row.identities_found = 1
    session.commit()
    monkeypatch.setattr(source_runs, "resolve_scraping_entry", lambda *a, **k: _entry())
    seen: list[str] = []

    def search(query: str) -> list[SearchResult]:
        seen.append(query)
        return _results("https://boltic.example/careers")

    summary = run_careers_search(
        session,
        run,
        search=search,
        pacer=_pacer(),
        client=FakeClient(pages={"https://boltic.example/careers": "<html></html>"}),
        vocabulary=object(),
    )

    assert seen == [build_careers_query("Boltic Oy")]
    assert [row.status for row in _rows(session, run)] == ["completed", "completed"]
    assert summary["pages"] == 1


def test_a_company_reviewed_while_queued_is_satisfied_without_a_search(session):
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    # The human answered the review gate between enqueue and execution.
    company.careers_url = "https://acme.example/ura"
    session.commit()

    summary = run_careers_search(
        session,
        run,
        search=lambda query: pytest.fail("a resolved company is not searched"),
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
    )

    session.expire_all()
    row = _rows(session, run)[0]
    assert row.status == "completed"
    assert row.error == "already has a hiring source"
    assert session.get(Company, company.id).careers_url == "https://acme.example/ura"
    assert summary["already_resolved"] == 1


def test_a_company_accepted_over_an_entry_is_satisfied_without_re_resolving(session):
    """The pin rule: an approved entry is a decision, not a guess to redo.

    "Accept entry only" leaves ``careers_url`` NULL, so the old careers-url-only
    skip rule would have re-resolved the row on the next run and silently
    replaced the page the owner approved.
    """
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    # The owner accepted the resolved entry between enqueue and execution.
    company.status = "accepted"
    company.open_positions_url = "https://acme.example/careers/avoimet"
    company.open_positions_landing_url = "https://acme.example/careers"
    session.commit()

    summary = run_careers_search(
        session,
        run,
        search=lambda query: pytest.fail("an accepted row with an entry is not searched"),
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
    )

    session.expire_all()
    row = _rows(session, run)[0]
    assert row.status == "completed"
    assert row.error == "already has a hiring source"
    stored = session.get(Company, company.id)
    assert stored.careers_url is None
    assert stored.open_positions_url == "https://acme.example/careers/avoimet"
    assert summary["already_resolved"] == 1


def test_three_fresh_empty_searches_pause_the_run_and_keep_the_rest_queued(session):
    """Block-streak ladder: a *throttled free* backend, not the paid one."""
    companies = [_company(session, f"Acme {index} Oy") for index in range(4)]
    run = _started_run(session, companies)

    summary = run_careers_search(
        session,
        run,
        search=lambda query: [],
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="ddgs",
    )

    rows = _rows(session, run)
    assert [row.status for row in rows] == ["zero_yield", "zero_yield", "zero_yield", "queued"]
    stored = session.get(JobRun, run.id)
    assert stored.status == "paused"
    assert "blocked" in stored.error
    assert summary["stopped"] == "blocked"
    # The resume point is a real checkpoint, not a restart.
    assert json.loads(stored.summary)["misses"] == 3


def test_a_search_failure_is_a_failed_row_not_a_company_miss(session):
    """P4: the query produced no answer; that is not evidence about the company."""
    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])

    def failing_search(query: str):
        raise SearchError(query, SEARCH_FAILURE_NO_ENGINE, "RuntimeError: Received 429")

    summary = run_careers_search(
        session,
        run,
        search=failing_search,
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="ddgs",
    )

    row = _rows(session, run)[0]
    assert row.status == "failed"
    assert row.error is not None
    assert row.error.startswith(f"search failed ({SEARCH_FAILURE_NO_ENGINE}) for")
    assert "Acme Oy careers open positions" in row.error
    assert summary["search_failures"] == 1
    assert summary["failures"] == 1
    assert summary["misses"] == 0
    # A failed query never becomes a cache entry, so a rerun searches again.
    assert session.query(SearchCache).count() == 0


def test_a_refused_key_pauses_the_run_with_every_row_still_queued(session):
    """RF4: a dead key fails the same for every company; do not walk the manifest."""
    companies = [_company(session, f"Acme {index} Oy") for index in range(3)]
    run = _started_run(session, companies)
    calls: list[str] = []

    def refused_search(query: str):
        calls.append(query)
        raise SearchRefusedError("Brave search refused the request (HTTP 402)")

    summary = run_careers_search(
        session,
        run,
        search=refused_search,
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="brave",
    )

    assert len(calls) == 1
    assert [row.status for row in _rows(session, run)] == ["queued", "queued", "queued"]
    stored = session.get(JobRun, run.id)
    assert stored.status == "paused"
    assert "refused" in stored.error
    assert summary["stopped"] == "refused"
    assert summary["failures"] == 0


def test_three_consecutive_search_failures_pause_the_run(session):
    """A run of failed queries is a block: stop burning the manifest."""
    companies = [_company(session, f"Acme {index} Oy") for index in range(4)]
    run = _started_run(session, companies)

    def failing_search(query: str):
        raise SearchError(query, SEARCH_FAILURE_NO_ENGINE, "RuntimeError: Received 403")

    summary = run_careers_search(
        session,
        run,
        search=failing_search,
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="ddgs",
    )

    rows = _rows(session, run)
    assert [row.status for row in rows] == ["failed", "failed", "failed", "queued"]
    stored = session.get(JobRun, run.id)
    assert stored.status == "paused"
    assert "blocked" in stored.error
    assert summary["stopped"] == "blocked"
    assert json.loads(stored.summary)["search_failures"] == 3


def test_empty_searches_do_not_pause_a_paid_backend(session):
    """A paid backend answering "nothing matched" is a miss, not a block.

    The block-streak ladder exists to back off an IP-throttled free engine; on
    a paid backend an empty answer is evidence about the company, and pausing
    the run after three would be a false stop.
    """
    companies = [_company(session, f"Acme {index} Oy") for index in range(4)]
    run = _started_run(session, companies)

    summary = run_careers_search(
        session,
        run,
        search=lambda query: [],
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="brave",
    )

    assert [row.status for row in _rows(session, run)] == ["zero_yield"] * 4
    assert summary["stopped"] is None
    assert session.get(JobRun, run.id).status == "running"


def test_a_cached_empty_result_does_not_count_as_a_block(session):
    """A cached empty is a real miss; only live requests signal a block."""

    companies = [_company(session, f"Acme {index} Oy") for index in range(4)]
    run = _started_run(session, companies)
    for company in companies:
        session.add(
            SearchCache(
                company_id=company.id,
                query=build_careers_query(company.name),
                payload=json.dumps({"backend": "ddgs", "results": []}),
                fetched_at=datetime(2026, 11, 1),
            )
        )
    session.commit()

    summary = run_careers_search(
        session,
        run,
        search=lambda query: pytest.fail("a cache hit must not search"),
        pacer=_pacer(),
        client=FakeClient(),
        vocabulary=object(),
        backend="ddgs",
    )

    assert [row.status for row in _rows(session, run)] == ["zero_yield"] * 4
    assert summary["stopped"] is None
    assert session.get(JobRun, run.id).status == "running"


def test_execute_run_dispatches_careers_search(session, monkeypatch):
    """The worker knows the run type and reports its summary (B1/B2 seam)."""
    from jobsearch_worker import consumer

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])
    seen: dict[str, object] = {}

    def fake_workflow(db_session, job_run, **kwargs):
        seen["run_type"] = job_run.run_type
        row = db_session.query(RunSource).filter(RunSource.run_id == job_run.id).one()
        row.status = "completed"
        db_session.commit()
        return {"run_type": "careers_search", "pages": 1}

    monkeypatch.setattr(consumer, "run_careers_search", fake_workflow)
    execute_run(session, run)

    stored = session.get(JobRun, run.id)
    assert seen["run_type"] == "careers_search"
    assert stored.status == "completed"
    assert json.loads(stored.summary)["pages"] == 1


def test_execute_run_leaves_a_blocked_run_paused(session, monkeypatch):
    """A pause is a resumable checkpoint, never reported as completed."""
    from jobsearch_worker import consumer

    company = _company(session, "Acme Oy")
    run = _started_run(session, [company])

    def blocked_workflow(db_session, job_run, **kwargs):
        job_run.status = "paused"
        job_run.error = "search blocked: 3 consecutive empty results; resume to continue"
        db_session.commit()
        return {"run_type": "careers_search", "stopped": "blocked"}

    monkeypatch.setattr(consumer, "run_careers_search", blocked_workflow)
    execute_run(session, run)

    stored = session.get(JobRun, run.id)
    assert stored.status == "paused"
    assert "blocked" in stored.error
    assert stored.finished_at is None
