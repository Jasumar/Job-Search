"""Source-driven runs (M1): ``careers_search`` (B2) and ``name_collect`` (B3).

One run asks a search engine for one careers page per company and resolves the
page that actually yields postings. What the run records is the 4.0d entry —
``open_positions_url`` with its landing page and link text, written through
``record_open_positions_entry`` — and it never writes ``careers_url``: that
field belongs to the human review gate (``review_discovery``). A miss lands on
the run row, never on the company.

A ``name_collect`` run reads one page per source and writes the company names
that page lists as ``pending`` candidate rows. A page is read by the recipe
interpreter — a stored verified recipe where one exists, a single authored one
otherwise (``run_name_collect``). A page whose markup carries no name list
(an article, prose) gets one model call that mines names from its text (PX4);
nothing is stored as a recipe.

Invariants:

- one company (``careers_search``) or one source (``name_collect``) at a time
  in manifest order; that boundary is the checkpoint, so pause and cancel are
  honoured there and a resumed run picks up from the first row still ``queued``
  or ``running``;
- a cache hit costs no request and no pacing, and only a *fresh* empty result
  counts towards the block streak — a cached empty is a real miss, not a block;
- one search request per company. The candidate pages are read from that single
  result set (never a second query, never a batch), so trying another employer
  page costs a fetch, not a paid search;
- a cache row written by a different search backend is not a hit: a paid
  backend must not replay the free engines' blocked-run misses;
- a persistent block pauses the run (resumable, with the reason in ``error``)
  instead of failing it, so the remaining companies are not lost;
- no LLM call per company of a careers search: the query and the candidate rule
  are deterministic (plan-source-runs.md §Locked decisions, cost ranks);
- no LLM call per source replay: authoring is one call at most, and only when
  the source has no verified recipe (or it drifted out of tolerance);
- the only other model call is the PX4 name mining, one per source, only when
  the page has no name list for a recipe to read.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobsearch_core.company_source import has_source
from jobsearch_core.config import EntryVocabulary, Settings, load_defaults, load_entry_vocabulary
from jobsearch_core.db.models import Company, CompanyRecipe, JobRun, RunSource, Source
from jobsearch_core.db.source_evidence import compare_to_previous
from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.listing import Capture, author_input, capture_page, name_reference
from jobsearch_core.ingest.llm_protocol import JsonCompleter
from jobsearch_core.ingest.recipe_author import AuthorResult, author_recipe
from jobsearch_core.ingest.recipe_replay import (
    ReplayOutcome,
    load_verified_recipe,
    mark_stale,
    replay_stored_recipe,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.entry_resolution import EntryReport, EntryResolution
from jobsearch_core.ingest.scrapers.pipeline import (
    record_open_positions_entry,
    resolve_scraping_entry,
)
from jobsearch_core.ingest.scrapers.sitemap import is_sitemap_document
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger
from jobsearch_core.search.careers_search import (
    PAID_SEARCH_BACKENDS,
    AdaptivePacer,
    build_careers_query,
    cached_search,
)
from jobsearch_core.search.client import (
    SearchClient,
    SearchError,
    SearchRefusedError,
    SearchResult,
    build_search_client,
)
from jobsearch_core.search.discover import (
    _company_key,
    mine_page_text,
    rank_company_sources_with_reason,
)

logger = get_logger(__name__)


# Consecutive *fresh* empty searches that mean the engine is blocking us rather
# than answering. Two would trip on two genuinely careerless companies in a row;
# four wastes half a batch before backing off.
BLOCK_STREAK = 3

# Candidate start pages one company may try before the row is a miss. The
# search already paid for these results, so a second employer page costs one
# static fetch, never another search request. Two keeps the worst case at ten
# descent fetches per company (two pages x MAX_FETCHES_PER_COMPANY).
MAX_CANDIDATE_PAGES = 2

# Rows a resumed run still has to do. ``running`` is included so a row left
# behind by a crashed worker is retried instead of stranded.
_PENDING = ("queued", "running")


@dataclass
class _Attempt:
    """What one company's step produced, before the row is written.

    There is no ``extraction`` payload: ``run_sources.extraction`` is a flag
    (``recipe``/``llm``) constrained by ``ck_run_sources_extraction``, not a
    metadata bag. A hit is recorded on the company (``open_positions_url`` and
    its provenance); a miss is the row's ``error``. ``trace`` is the entry
    descent's evidence — ``EntryReport.as_trace()`` of the decisive descent plus
    ``candidate_pages`` (every start page tried, in rank order) — written to
    ``run_sources.entry_trace`` so the review of a miss does not need the log.
    """

    entry: EntryResolution | None = None
    miss_reason: str | None = None
    trace: dict[str, Any] | None = None
    from_cache: bool = False
    raw_results: int = 0


def run_careers_search(
    session: Session,
    run: JobRun,
    *,
    search: Callable[[str], list[SearchResult]] | None = None,
    pacer: AdaptivePacer | None = None,
    client: PoliteClient | None = None,
    vocabulary: EntryVocabulary | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    """Execute a ``careers_search`` run; returns the run summary.

    A pause or cancel request leaves the run in that state (never "completed")
    and the summary in ``run.summary`` when the stop was ours. Everything
    exceptional per company is a ``failed`` row: the run itself only fails on a
    summarising bug.
    """
    backend = backend or load_defaults().search_backend
    search_client: SearchClient | None = None
    if search is None:
        search_client = build_search_client(backend)
        search_fn: Callable[[str], list[SearchResult]] = search_client.search
    else:
        search_fn = search
    pacer = pacer if pacer is not None else AdaptivePacer.for_backend(backend)
    owns_client = client is None
    client = client if client is not None else PoliteClient()
    vocabulary = vocabulary if vocabulary is not None else load_entry_vocabulary()

    rows = session.execute(
        select(RunSource, Company)
        .join(Company, Company.id == RunSource.company_id)
        .where(RunSource.run_id == run.id)
        .order_by(RunSource.id)
    ).all()

    summary: dict[str, Any] = {
        "run_type": "careers_search",
        "companies": len(rows),
        "pages": 0,
        "misses": 0,
        "failures": 0,
        "search_failures": 0,
        "cached": 0,
        "already_resolved": 0,
        "stopped": None,
    }
    # A previous attempt may have paused on a block; that reason is stale now,
    # and it must not sit on a run that is visibly working again.
    if run.error:
        run.error = None
        session.commit()
    streak = 0
    try:
        for row, company in rows:
            if row.status not in _PENDING:
                continue
            state = _checkpoint(session, run)
            if state is not None:
                summary["stopped"] = state
                break
            if has_source(company):
                # A source already exists: either the review gate answered while
                # this run was queued (careers_url), or the row was accepted over
                # a resolved entry. Both mean the owner has decided; the entry is
                # the page the scrape uses, so re-resolving it would undo them.
                row.status = "completed"
                row.error = "already has a hiring source"
                row.finished_at = _now()
                summary["already_resolved"] += 1
                session.commit()
                continue

            row_id = row.id
            row.status = "running"
            row.started_at = _now()
            row.error = None
            session.commit()

            try:
                attempt = _attempt_company(
                    session,
                    company,
                    search=search_fn,
                    pacer=pacer,
                    client=client,
                    vocabulary=vocabulary,
                    backend=backend,
                )
            except SearchRefusedError as exc:
                # RF4: the key or quota is dead for every company. Put this row
                # back in the queue and pause, so a resume picks it up again
                # instead of walking the manifest into `failed` rows.
                logger.warning("careers_search: backend refused company #%s: %s", company.id, exc)
                session.rollback()
                row = session.get(RunSource, row_id)
                row.status = "queued"
                row.started_at = None
                session.commit()
                summary["stopped"] = "refused"
                _pause_for_refusal(session, run, summary, str(exc))
                break
            except SearchError as exc:
                # P4: the query itself produced no answer (block, throttle,
                # soft-error page). That is not evidence about the company, so
                # the row names a search failure instead of reading as a miss,
                # and it counts toward the block streak like an empty.
                logger.warning(
                    "careers_search: company #%s search failed: %s", company.id, exc
                )
                session.rollback()
                row = session.get(RunSource, row_id)
                row.status = "failed"
                row.error = str(exc)
                row.finished_at = _now()
                summary["failures"] += 1
                summary["search_failures"] += 1
                session.commit()
                streak += 1
                pacer.on_block()
                if streak >= BLOCK_STREAK:
                    summary["stopped"] = "blocked"
                    _pause_for_block(session, run, summary, streak)
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - one company, not the run
                logger.warning(
                    "careers_search: company #%s failed: %s", company.id, exc, exc_info=True
                )
                session.rollback()
                row = session.get(RunSource, row_id)
                row.status = "failed"
                row.error = f"{type(exc).__name__}: {exc}"
                row.finished_at = _now()
                summary["failures"] += 1
                session.commit()
                continue

            if attempt.entry is None:
                row.status = "zero_yield"
                row.error = attempt.miss_reason
                summary["misses"] += 1
            else:
                record_open_positions_entry(company.id, attempt.entry)
                row.status = "completed"
                row.identities_found = 1
                summary["pages"] += 1
            row.entry_trace = json.dumps(attempt.trace, sort_keys=True) if attempt.trace else None
            row.finished_at = _now()
            session.commit()

            if attempt.from_cache:
                summary["cached"] += 1
            elif attempt.raw_results:
                streak = 0
                pacer.on_clean()
            elif backend not in PAID_SEARCH_BACKENDS:
                # An empty result is evidence of a block only for a free engine.
                # A paid backend answering "nothing matched" is a real miss, and
                # pausing the run over three such companies would be a false stop.
                streak += 1
                pacer.on_block()

            if streak >= BLOCK_STREAK:
                summary["stopped"] = "blocked"
                _pause_for_block(session, run, summary, streak)
                break
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_client:
            client.close()
        if search_client is not None:
            search_client.close()

    return summary


def _attempt_company(
    session: Session,
    company: Company,
    *,
    search: Callable[[str], list[SearchResult]],
    pacer: AdaptivePacer,
    client: PoliteClient,
    vocabulary: EntryVocabulary,
    backend: str,
) -> _Attempt:
    """Search once, then try the ranked employer pages until one yields postings.

    Exactly one search request per company: ``cached_search`` is called once and
    every candidate page comes from that one result set, so widening the
    candidate rule never widens the paid request count. A second candidate is
    only fetched when the first descent misses, and an unreachable candidate is
    recorded in the trace and skipped — the row is ``failed`` only when no
    candidate page could be fetched at all.

    Exceptions that are not about reachability propagate: a broken extractor is a
    failed row, which is different information from "this company has no page".
    """
    query = build_careers_query(company.name)
    found = cached_search(
        session,
        company_id=company.id,
        query=query,
        search=search,
        pacer=pacer,
        backend=backend,
    )
    attempt = _Attempt(from_cache=found.from_cache, raw_results=len(found.results))
    if not found.results:
        attempt.miss_reason = "no search results"
        return attempt

    candidates, refusals = rank_company_sources_with_reason(
        found.results, company.name, allow_employer_root=True
    )
    if not candidates:
        # P3: a miss caused by an aggregator/board host names the host, so the
        # row does not read like a company with no careers page.
        attempt.miss_reason = (
            "; ".join(refusals[:3]) or "no employer careers page among the results"
        )
        return attempt

    pages: list[dict[str, Any]] = []
    reports: list[EntryReport] = []
    first_error: Exception | None = None
    for rank, candidate in enumerate(candidates[:MAX_CANDIDATE_PAGES]):
        try:
            html = client.fetch(candidate.url)
        except Exception as exc:  # noqa: BLE001 - one candidate, not the company
            logger.warning(
                "careers_search: could not fetch candidate %s: %s", candidate.url, exc
            )
            first_error = first_error or exc
            pages.append(
                {
                    "url": candidate.url,
                    "rank": rank,
                    "outcome": "unreachable",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        report = resolve_scraping_entry(
            candidate.url,
            client=client,
            vocabulary=vocabulary,
            page_html=html,
        )
        reports.append(report)
        pages.append(
            {
                "url": candidate.url,
                "rank": rank,
                "outcome": "resolved" if report.resolution is not None else "no_entry",
                "reason": report.reason,
            }
        )
        if report.resolution is not None:
            break

    if not reports:
        # Nothing was even readable: that is a failed row, not a company with
        # no careers page.
        assert first_error is not None
        raise first_error

    # The decisive descent is the last one: the hit, or the final miss. Its
    # trace explains the entry; ``candidate_pages`` says which start pages were
    # tried before it and why the earlier ones did not win.
    decisive = reports[-1]
    trace = decisive.as_trace()
    trace["candidate_pages"] = pages
    attempt.trace = trace
    if decisive.resolution is None:
        attempt.miss_reason = "; ".join(
            dict.fromkeys(report.reason for report in reports)
        )
        return attempt

    attempt.entry = decisive.resolution
    return attempt


def _checkpoint(session: Session, run: JobRun) -> str | None:
    """Read the run's control state fresh; ``paused``/``cancelled`` stop the loop.

    Committing per row ends the transaction, so this refresh starts a new one
    and sees the API's write instead of a stale snapshot.
    """
    session.refresh(run)
    if run.status == "paused":
        return "paused"
    if run.status == "cancelled_requested":
        return "cancelled"
    return None


def _pause_for_block(session: Session, run: JobRun, summary: dict[str, Any], streak: int) -> None:
    """Leave the run resumable with a reason, rather than failing it.

    ``execute_run`` never reports "completed" over a paused run, so the summary
    is written here — there is no later terminal write to carry it.
    """
    run.status = "paused"
    run.error = (
        f"search blocked: {streak} consecutive searches without an answer; resume to continue"
    )
    run.summary = json.dumps(summary, sort_keys=True)
    session.commit()
    logger.warning("Run %s paused after %d consecutive searches without an answer", run.id, streak)


def _pause_for_refusal(
    session: Session, run: JobRun, summary: dict[str, Any], reason: str
) -> None:
    """Pause on a refused key or spent quota; the reason is the backend's own."""
    run.status = "paused"
    run.error = f"search refused: {reason}; fix the key or quota, then resume"
    run.summary = json.dumps(summary, sort_keys=True)
    session.commit()
    logger.warning("Run %s paused: search backend refused the request", run.id)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --- name_collect (B3) ------------------------------------------------------------

# Sources processed before names are written and the run checks pause/cancel.
# One is the safe default: a batch is the unit the owner can interrupt.
DEFAULT_BATCH_SIZE = 1
# A source earns a stored recipe (the plan's "saved source") only when its page
# carried at least this many names and the replay check held.
SAVE_MIN_IDENTITIES = 10
# The replay check passes above this share of the source's last verified count
# (plan-source-runs §Locked decisions; pure counts, never a URL diff).
REPLAY_TOLERANCE = 0.75
# One authoring trial's raw model reply is stored on the run source; a runaway
# reply is cut here rather than filling the row (the reason it failed is the
# part that is read, the body is the supporting evidence).
_AUTHOR_TRACE_BODY_LIMIT = 4000
# Most names one unstructured page may yield through PX4 mining (a ranking of
# ~100 companies is the largest expected source).
MINED_NAME_LIMIT = 150

# Suffix -> country, only where the suffix is unambiguous. A name whose last
# word is not in here gets no country: guessing from "Ltd"/"Inc"/"LLC" would
# contradict the source. The table grows only when live data shows it must
# (plan-source-runs §Open work 5).
_COUNTRY_SUFFIX = {
    "sia": "LV",
    "oy": "FI",
    "oyj": "FI",
    "ab": "SE",
    "gmbh": "DE",
}


@dataclass
class _Collected:
    """What one source's step produced, before the manifest row is written."""

    names: list[str]
    extraction: str | None  # 'recipe' (stored) or 'llm' (authored this run)
    recipe_id: int | None
    verified_count: int
    # The replay check's wording, which is what ``sources.replay_check`` keeps.
    note: str | None = None
    # Why the row carries names the source did not earn a recipe for. The row's
    # ``error`` is this field, so a fallback is never silent.
    warning: str | None = None


@dataclass
class _SourceNames:
    """One source's names waiting for the batch boundary."""

    row_id: int
    source_id: int
    names: list[str]


def run_name_collect(
    session: Session,
    run: JobRun,
    *,
    client: PoliteClient | None = None,
    llm: JsonCompleter | None = None,
) -> dict[str, Any]:
    """Execute a ``name_collect`` run; returns the run summary.

    A pause or cancel request leaves the run in that state (never "completed").
    Everything exceptional per source is a ``failed`` row; the run itself only
    fails on a summarising bug.

    Names are written per batch, not per source: the batch boundary is where
    the canonical-key dedupe runs and where pause/cancel take effect, and a
    stop flushes whatever the finished sources found so far.
    """
    params = json.loads(run.params)
    batch_size = int(params.get("batch_size") or DEFAULT_BATCH_SIZE)
    page_ceiling = params.get("page_ceiling")
    owns_client = client is None
    client = client if client is not None else PoliteClient()
    llm = llm if llm is not None else _authoring_llm()

    rows = session.execute(
        select(RunSource, Source)
        .join(Source, Source.id == RunSource.source_id)
        .where(RunSource.run_id == run.id)
        .order_by(RunSource.id)
    ).all()

    summary: dict[str, Any] = {
        "run_type": "name_collect",
        "sources": len(rows),
        "completed": 0,
        "zero_yield": 0,
        "failures": 0,
        "authored": 0,
        "replayed": 0,
        "identities": 0,
        "added": 0,
        "duplicates": 0,
        "stopped": None,
    }
    # A reason from an earlier pause is stale once the run is visibly working.
    if run.error:
        run.error = None
        session.commit()

    batch: list[_SourceNames] = []
    try:
        for row, source in rows:
            if row.status not in _PENDING:
                continue
            state = _checkpoint(session, run)
            if state is not None:
                _flush_batch(session, batch, summary)
                batch = []
                summary["stopped"] = state
                break

            row_id = row.id
            row.status = "running"
            row.started_at = _now()
            row.error = None
            session.commit()

            try:
                collected = _collect_source(
                    session,
                    row,
                    source,
                    client=client,
                    llm=llm,
                    page_ceiling=page_ceiling,
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001 — one source, not the run
                logger.warning(
                    "name_collect: source #%s failed: %s", source.id, exc, exc_info=True
                )
                session.rollback()
                row = session.get(RunSource, row_id)
                row.status = "failed"
                row.error = f"{type(exc).__name__}: {exc}"
                # A failed authoring's trials are evidence, not state: the
                # rollback above drops the flushed copy, so the exception
                # carries it here.
                row.author_trace = getattr(exc, "trace", None) or row.author_trace
                row.finished_at = _now()
                summary["failures"] += 1
                session.commit()
                continue

            row = session.get(RunSource, row_id)
            row.identities_found = collected.verified_count
            row.extraction = collected.extraction
            row.recipe_id = collected.recipe_id
            if collected.names:
                row.status = "completed"
                row.error = collected.warning
                summary["completed"] += 1
            else:
                row.status = "zero_yield"
                row.error = collected.warning or "the page yielded no company names"
                summary["zero_yield"] += 1
            row.finished_at = _now()
            summary["identities"] += collected.verified_count
            session.commit()

            batch.append(_SourceNames(row.id, source.id, collected.names))
            if len(batch) >= batch_size:
                _flush_batch(session, batch, summary)
                batch = []
        if batch:
            _flush_batch(session, batch, summary)
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_client:
            client.close()

    return summary


def _collect_source(
    session: Session,
    row: RunSource,
    source: Source,
    *,
    client: PoliteClient,
    llm: JsonCompleter | None,
    page_ceiling: int | None,
    summary: dict[str, Any],
) -> _Collected:
    """Read one source page: replay its verified recipe, else author one.

    A stored recipe that replays empty is stale and is re-authored. One that
    replays but shrinks past ``REPLAY_TOLERANCE`` of the source's last verified
    count is drift: the recipe is marked stale and re-authored, and if that
    authoring fails the drifted names are still used (the page did list them)
    with the drift written on the row — losing a page's names because a new
    recipe could not be written would be a worse failure than a stale recipe.
    """
    baseline = source.last_verified_count
    fallback: _Collected | None = None

    recipe = load_verified_recipe(session, target_url=source.url)
    if recipe is not None:
        outcome = replay_stored_recipe(
            session,
            run_source_id=row.id,
            recipe_row=recipe,
            page_url=source.url,
            client=client,
            llm=None,
            page_ceiling=page_ceiling,
        )
        if outcome.ok:
            passed, note = _replay_check(baseline, outcome.verified_count)
            note += _page_note(outcome)
            drift = compare_to_previous(session, row.id)
            if drift is not None and drift.has_drift:
                note = f"{note}, drift +{len(drift.added)}/-{len(drift.removed)}"
            names = _posting_titles(outcome.postings)
            if passed:
                summary["replayed"] += 1
                _save_source(session, source, recipe.id, outcome.verified_count, note, passed=True)
                return _Collected(names, "recipe", recipe.id, outcome.verified_count, note)

            fallback = _Collected(
                names,
                "recipe",
                recipe.id,
                outcome.verified_count,
                note,
                warning=f"recipe drift: {note}; re-authoring",
            )
        else:
            fallback = None
        mark_stale(session, recipe)
        session.flush()

    if llm is None:
        if fallback is not None:
            fallback.warning = f"{fallback.warning}; re-authoring skipped: no model configured"
            _save_source(session, source, fallback.recipe_id, fallback.verified_count, fallback.note, passed=False)
            _keep_baseline(source, baseline)
            return fallback
        raise RuntimeError(
            "no verified recipe and no model configured: cannot author a listing recipe"
        )

    captured = capture_page(source.url, client)
    if is_sitemap_document(captured.static):
        # A sitemap is not a listing for a name_collect source: names from
        # <loc> slugs are guesses. Fail the row before any authoring trial is
        # paid for, with the fix the owner can act on.
        raise RuntimeError("sitemap source: not supported — paste the listing page")
    reference = name_reference(captured.artifacts)
    if not reference.names:
        if fallback is not None:
            fallback.warning = f"{fallback.warning}; re-authoring impossible: {reference.note}"
            _save_source(session, source, fallback.recipe_id, fallback.verified_count, fallback.note, passed=False)
            _keep_baseline(source, baseline)
            return fallback
        return _mine_unstructured(source.url, captured, llm, reference.note)

    inp = author_input(source.url, captured, reference, client.fetch_json, client.fetch)
    result = author_recipe(session, None, inp, llm)
    trace = _author_trace(result)
    row.author_trace = trace
    session.flush()
    if result.status != "verified" or result.recipe_id is None:
        reason = result.trials[-1].reason if result.trials else "no trial ran"
        if fallback is not None:
            fallback.warning = f"{fallback.warning}; re-authoring failed: {reason}"
            _save_source(session, source, fallback.recipe_id, fallback.verified_count, fallback.note, passed=False)
            _keep_baseline(source, baseline)
            return fallback
        # The caller rolls the transaction back to mark the row failed, which
        # would take the trace with it: carry it on the exception.
        raise AuthoringError(reason, trace)
    summary["authored"] += 1
    session.commit()

    stored = session.get(CompanyRecipe, result.recipe_id)
    if stored is None:
        raise RuntimeError(f"authored recipe #{result.recipe_id} is not readable")
    outcome = replay_stored_recipe(
        session,
        run_source_id=row.id,
        recipe_row=stored,
        page_url=source.url,
        client=client,
        llm=None,
        page_ceiling=page_ceiling,
    )
    if not outcome.ok:
        raise RuntimeError(
            f"authored recipe did not replay: {outcome.error or outcome.status}"
        )
    # The new recipe is the baseline now: there is nothing older to compare to.
    passed, note = _replay_check(None, outcome.verified_count)
    note += _page_note(outcome)
    names = _posting_titles(outcome.postings)
    saved = _save_source(session, source, stored.id, outcome.verified_count, note, passed=passed)
    warning = None
    if not saved:
        warning = f"not saved: {len(names)} names, needs {SAVE_MIN_IDENTITIES} and a passing replay check"
    return _Collected(names, "llm", stored.id, outcome.verified_count, note, warning)


def _mine_unstructured(
    url: str, captured: Capture, llm: JsonCompleter, reference_note: str
) -> _Collected:
    """PX4: one model call reads names from a page that has no list to replay.

    No recipe is authored or saved and ``verified_count`` stays 0: nothing was
    verified against the page's structure, so the source never earns a
    baseline. The names are ordinary ``pending`` candidates for the owner. The
    row's ``extraction`` is ``llm`` and its ``recipe_id`` stays empty; the
    warning says so, so the path is never silent.
    """
    page = captured.rendered or captured.static
    mined, _ = mine_page_text(
        url, page, llm, Settings().llm_cache_dir, max_companies=MINED_NAME_LIMIT  # type: ignore[arg-type]
    )
    names = [" ".join(item.company_name.split()) for item in mined]
    if not names:
        return _Collected(
            [],
            None,
            None,
            0,
            reference_note,
            warning=f"no company names in the page markup or its text: {reference_note}",
        )
    return _Collected(
        names,
        "llm",
        None,
        0,
        reference_note,
        warning=f"{len(names)} names mined by the model from page text; no recipe saved",
    )


def _posting_titles(postings: tuple[ScrapedJob, ...]) -> list[str]:
    """A listing replay's items are company names, not job titles."""
    return [" ".join(job.title.split()) for job in postings if job.title and job.title.strip()]


def _replay_check(baseline: int | None, count: int) -> tuple[bool, str]:
    """Compare a replay's count with the source's last verified one.

    No baseline (first verified run for this source) passes without comparing:
    there is nothing to have drifted from.
    """
    if not baseline:
        return True, f"{count} identities (first verified run, no baseline)"
    share = count / baseline
    note = f"{count}/{baseline} identities ({share:.0%})"
    if share > REPLAY_TOLERANCE:
        return True, note
    return False, f"replay drifted: {note}, tolerance {REPLAY_TOLERANCE:.0%}"


class AuthoringError(RuntimeError):
    """A listing could not be authored; ``trace`` is the trials' evidence.

    Raised where the row would otherwise lose that evidence to the caller's
    rollback, so the caller reads it off the exception instead.
    """

    def __init__(self, reason: str | None, trace: str) -> None:
        super().__init__(f"recipe authoring failed: {reason}")
        self.trace = trace


def _author_trace(result: AuthorResult) -> str:
    """One authoring call's trials, as stored on ``run_sources.author_trace``.

    A pass on the third trial and a plain failure need the same evidence: which
    body was proposed and what the replay said about it. The body is the model's
    raw reply — the reason alone cannot be re-diagnosed after the run.

    The row is an M1 listing, so there is exactly one attempt (a listing carries
    no trial state); the list is kept because the company path may author here
    later and an empty list would be indistinguishable from a missing trace.
    """
    payload = {
        "status": result.status,
        "kind": result.kind,
        "recipe_id": result.recipe_id,
        "trials": [
            {
                "number": trial.number,
                "passed": trial.passed,
                "reason": trial.reason,
                "body": (trial.body or "")[:_AUTHOR_TRACE_BODY_LIMIT],
            }
            for trial in result.trials
        ],
    }
    return json.dumps(payload, sort_keys=True)


def _page_note(outcome: ReplayOutcome) -> str:
    """Why a paginated ``dom`` replay stopped before running out of pages.

    ``single_page`` and ``no_signal`` are the ordinary ends and say nothing.
    Every other stop is a fact about the listing that the count cannot show — a
    page budget reached, a page repeating, a next link off ``allowed_hosts``, a
    fetch that failed. Without it a short list reads as the site having few
    postings.
    """
    stop = outcome.page_stop
    if stop is None or stop in ("single_page", "no_signal"):
        return ""
    return f" · paging stopped: {stop}"


def _keep_baseline(source: Source, baseline: int | None) -> None:
    """Undo ``record_verified_urls``' count mirror for a replay that lost.

    ``record_verified_urls`` always writes the count it saw. A drifted or
    fallback replay must not become the next run's baseline, or the drift it
    just reported would be unmeasurable the second time.
    """
    source.last_verified_count = baseline


def _save_source(
    session: Session,
    source: Source,
    recipe_id: int | None,
    count: int,
    note: str | None,
    *,
    passed: bool,
) -> bool:
    """Record the check on the source and attach the recipe when it earned it.

    ``record_verified_urls`` already wrote ``last_verified_count`` during the
    replay; the ``saved`` verdict decides whether the source carries its recipe
    forward (plan-source-runs §Run flow 7): the replay check held AND the page
    listed at least ``SAVE_MIN_IDENTITIES`` names.
    """
    source.replay_check = note
    source.updated_at = _now()
    saved = passed and count >= SAVE_MIN_IDENTITIES and recipe_id is not None
    if saved:
        source.recipe_id = recipe_id
    session.flush()
    return saved


def _country_hint(name: str) -> str | None:
    """Country from a name's legal suffix, only where the suffix is unambiguous."""
    words = name.replace(",", " ").split()
    if not words:
        return None
    return _COUNTRY_SUFFIX.get(words[-1].casefold().strip("."))


def _flush_batch(session: Session, batch: list[_SourceNames], summary: dict[str, Any]) -> None:
    """Normalise one batch, dedupe by canonical key, insert pending companies.

    The key set is read from the whole ``companies`` table (every status): a
    name already known under any status is not re-inserted, and a name repeated
    inside the batch — or on two sources in it — counts once.

    Each inserted row carries ``run_id``: the run (and through its ``sources``
    row, the listing) that produced the candidate, so the review screen can say
    where the name came from (migration 0015).
    """
    if not batch:
        return
    known = {_company_key(name) for name in session.scalars(select(Company.name))}
    for item in batch:
        row = session.get(RunSource, item.row_id)
        if row is None:
            continue
        for raw in item.names:
            name = " ".join(str(raw).split())
            key = _company_key(name)
            if not key:
                continue
            if key in known:
                row.candidates_duplicate += 1
                summary["duplicates"] += 1
                continue
            known.add(key)
            session.add(
                Company(
                    name=name,
                    status="pending",
                    country=_country_hint(name),
                    run_id=row.run_id,
                )
            )
            row.candidates_added += 1
            summary["added"] += 1
    session.commit()


def _authoring_llm() -> JsonCompleter | None:
    """The author's model, or None without a key (authoring is then a recorded failure)."""
    settings = Settings()
    if not settings.openrouter_api_key:
        return None
    return LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )

