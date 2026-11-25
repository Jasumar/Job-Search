"""Database-backed in-process run consumer, intentionally independent of FastAPI."""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from jobsearch_core.company_source import has_source
from jobsearch_core.config import Settings, load_defaults, resolve_defaults
from jobsearch_core.db.models import Company, JobRun, RunSource, Source
from jobsearch_core.db.session import get_session
from jobsearch_core.ingest.scrapers.ats_links import is_ats_board_url
from jobsearch_core.profile_evidence import load_candidate_evidence
from jobsearch_core.run import (
    RunSummary,
    collect_workflow,
    extract_top_prefit,
    map_roles_workflow,
    match_jobs,
    parse_profile_workflow,
    run_workflow,
)
from jobsearch_core.source_runs import run_careers_search, run_name_collect
from sqlalchemy import select, update
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ActiveRunExistsError(RuntimeError):
    pass


class RunStateError(RuntimeError):
    """A run-control request that the run's current status forbids.

    ``current_status`` is the status that blocked the request, so the API can
    report it without a second read.
    """

    def __init__(self, run_id: int, current_status: str, action: str) -> None:
        super().__init__(f"Cannot {action} run {run_id} in status {current_status}")
        self.run_id = run_id
        self.current_status = current_status
        self.action = action


class UnknownCompaniesError(LookupError):
    def __init__(self, company_ids: list[int]) -> None:
        super().__init__(f"Unknown company ids: {company_ids}")
        self.company_ids = company_ids


class CompaniesHaveSourceError(ValueError):
    """Selected companies already have a hiring source the scrape can use.

    Not only a ``careers_url``: an accepted company with a resolved entry is
    satisfiable too (``company_source.has_source``).
    """

    def __init__(self, company_ids: list[int]) -> None:
        super().__init__(f"Companies already have a hiring source: {company_ids}")
        self.company_ids = company_ids


class InvalidSourceURLsError(ValueError):
    """Pasted source URLs that are not usable as M1 listing pages."""

    def __init__(self, urls: list[str], reason: str) -> None:
        super().__init__(f"Invalid source URLs ({reason}): {urls}")
        self.urls = urls
        self.reason = reason


class AtsSourceError(ValueError):
    """An ATS board URL pasted as a source (4.1e: boards are never sources)."""

    def __init__(self, urls: list[str]) -> None:
        super().__init__(f"ATS board URLs cannot be sources: {urls}")
        self.urls = urls


def enqueue_run(session: Session, run_type: str, params: dict[str, Any], triggered_by: str = "api") -> JobRun:
    active = session.scalar(
        select(JobRun).where(
            JobRun.run_type == run_type,
            # A paused run holds the run-type slot until resumed or cancelled
            # (plan-source-runs §Locked decisions §Concurrency).
            JobRun.status.in_(("queued", "running", "paused", "cancelled_requested")),
        )
    )
    if active is not None:
        raise ActiveRunExistsError(f"A {run_type} run is already active")
    run = JobRun(run_type=run_type, params=json.dumps(params), triggered_by=triggered_by)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def enqueue_careers_search(
    session: Session,
    company_ids: list[int],
    *,
    batch_size: int | None = None,
    page_ceiling: int | None = None,
    triggered_by: str = "api",
) -> JobRun:
    """Queue a ``careers_search`` run and its per-company manifest.

    The API is the only caller today. Validation happens before anything is
    written, so a rejected selection never leaves a run row behind:

    - ids are deduplicated in the order the owner selected them;
    - every id must exist;
    - companies that already have a ``careers_url`` are refused here rather
      than searched and discarded by the worker (the selection list is
      ``GET /companies?no_careers=1``; a stale list is a client error).

    ``run_sources`` rows start ``queued`` so the UI can show the whole manifest
    before the worker picks the run up.
    """
    ordered: list[int] = list(dict.fromkeys(company_ids))
    companies = {
        company.id: company
        for company in session.scalars(select(Company).where(Company.id.in_(ordered)))
    }
    unknown = [company_id for company_id in ordered if company_id not in companies]
    if unknown:
        raise UnknownCompaniesError(unknown)
    # A company that already has a hiring source is refused here rather than
    # searched and discarded by the worker (the selection list is
    # ``GET /companies?no_careers=1``; a stale list is a client error).
    satisfied = [company_id for company_id in ordered if has_source(companies[company_id])]
    if satisfied:
        raise CompaniesHaveSourceError(satisfied)

    params: dict[str, Any] = {"company_ids": ordered}
    if batch_size is not None:
        params["batch_size"] = batch_size
    if page_ceiling is not None:
        params["page_ceiling"] = page_ceiling

    run = enqueue_run(session, "careers_search", params, triggered_by=triggered_by)
    session.add_all(
        RunSource(run_id=run.id, company_id=company_id) for company_id in ordered
    )
    session.commit()
    session.refresh(run)
    return run


def enqueue_name_collect(
    session: Session,
    urls: list[str],
    *,
    batch_size: int | None = None,
    page_ceiling: int | None = None,
    triggered_by: str = "api",
) -> JobRun:
    """Queue a ``name_collect`` run over the owner's pasted listing URLs.

    Validation happens before anything is written, so a rejected paste never
    leaves a run or source row behind:

    - URLs are deduplicated in paste order and must be http(s);
    - an ATS board URL is refused: boards are reached from a verified employer
      careers page, never used as a source (4.1e provenance).

    Every URL gets a ``sources`` row (created here if new, reused by URL if
    already known) so the manifest can be ``run_sources`` rows from the start.
    A source row with no ``recipe_id`` is not "saved" in the plan's sense: the
    worker attaches the recipe only when a run finds 10+ identities and the
    replay check passes.
    """
    ordered = list(dict.fromkeys(url.strip() for url in urls if url and url.strip()))
    if not ordered:
        raise InvalidSourceURLsError([], "no URLs given")
    invalid = [
        url
        for url in ordered
        if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc
    ]
    if invalid:
        raise InvalidSourceURLsError(invalid, "not http(s) URLs")
    ats = [url for url in ordered if is_ats_board_url(url)]
    if ats:
        raise AtsSourceError(ats)

    now = datetime.utcnow()
    known = {source.url: source for source in session.scalars(select(Source))}
    for url in ordered:
        if url not in known:
            source = Source(url=url, created_at=now, updated_at=now)
            session.add(source)
            known[url] = source
    session.flush()

    params: dict[str, Any] = {"source_ids": [known[url].id for url in ordered]}
    if batch_size is not None:
        params["batch_size"] = batch_size
    if page_ceiling is not None:
        params["page_ceiling"] = page_ceiling

    run = enqueue_run(session, "name_collect", params, triggered_by=triggered_by)
    session.add_all(RunSource(run_id=run.id, source_id=known[url].id) for url in ordered)
    session.commit()
    session.refresh(run)
    return run


def claim_next_run(session: Session) -> JobRun | None:

    now = datetime.utcnow()
    candidate_id = session.scalar(select(JobRun.id).where(JobRun.status == "queued").order_by(JobRun.id).limit(1))
    if candidate_id is None:
        return None
    result = session.execute(
        update(JobRun)
        .where(JobRun.id == candidate_id, JobRun.status == "queued")
        .values(status="running", started_at=now)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.get(JobRun, candidate_id)


def finish_run(
    session: Session,
    run: JobRun,
    *,
    summary: RunSummary | dict[str, Any] | None = None,
    error: str | None = None,
    status: str | None = None,
) -> None:
    if status is None:
        status = "failed" if error is not None else "completed"
    run.status = status
    run.finished_at = datetime.utcnow()
    run.error = error
    if summary is not None:
        payload = asdict(summary) if is_dataclass(summary) else summary
        run.summary = json.dumps(payload, default=str)
    session.commit()


def request_cancel(session: Session, run_id: int) -> str:
    run = session.get(JobRun, run_id)
    if run is None:
        raise LookupError(run_id)
    if run.status in ("queued", "running", "paused"):
        run.status = "cancelled_requested"
        session.commit()
    return run.status


def request_pause(session: Session, run_id: int) -> str:
    """Pause a queued or running run. Returns the resulting status.

    ``paused`` doubles as the request and the state (like
    ``cancelled_requested``), so the worker can observe it without a second
    column. A paused run holds its run-type slot until resumed or cancelled.

    A queued run is paused before any work starts. A running run is only
    observed at the next checkpoint: legacy workflows (discover/collect/match/
    profile_parse) have none, so they finish their single call and
    ``execute_run`` then leaves the run paused instead of writing a terminal
    state. Resuming such a run re-executes it; those workflows are idempotent.
    """
    run = session.get(JobRun, run_id)
    if run is None:
        raise LookupError(run_id)
    if run.status in ("queued", "running"):
        run.status = "paused"
        session.commit()
        session.refresh(run)
    elif run.status != "paused":
        raise RunStateError(run_id, run.status, "pause")
    return run.status


def request_resume(session: Session, run_id: int) -> str:
    """Put a paused run back in the queue. Returns the resulting status."""
    run = session.get(JobRun, run_id)
    if run is None:
        raise LookupError(run_id)
    if run.status != "paused":
        raise RunStateError(run_id, run.status, "resume")
    run.status = "queued"
    run.started_at = None
    session.commit()
    session.refresh(run)
    return run.status


def _resolved_params(params: dict[str, Any]) -> dict[str, Any]:
    defaults = load_defaults()
    discover_names = (
        "search_backend", "max_baseline_queries", "max_companies", "expand_queries",
        "max_variants", "mine_listicles", "max_listicles", "max_companies_per_listicle",
    )
    overrides = {name: params.get(name) for name in discover_names}
    resolved = resolve_defaults(defaults, **overrides)
    resolved.update(
        {"country": params.get("country"), "role": params.get("role"), "scope": params.get("scope") or "tech"}
    )
    resolved["dry_run"] = params["dry_run"]
    return resolved


def execute_run(session: Session, run: JobRun) -> None:
    try:
        session.refresh(run)
        if run.status == "cancelled_requested":
            finish_run(session, run, status="cancelled")
            return
        if run.status == "paused":
            # Paused between claim and execution: do no work.
            return
        params = json.loads(run.params)
        if run.run_type == "discover":
            resolved = _resolved_params(params)
            resolved["max_variants_per_baseline"] = resolved.pop("max_variants")
            summary = run_workflow(**resolved, echo=None)
        elif run.run_type == "collect":
            summary = collect_workflow(
                dry_run=params["dry_run"],
                batch=params.get("batch"),
                refresh_known=params.get("refresh_known") if params.get("refresh_known") is not None else False,
                limit=params.get("limit"),
                company_ids=params.get("company_ids"),
                scope=params.get("scope") or "tech",
                echo=None,
                run_id=run.id,
            )
        elif run.run_type == "match":
            settings = Settings()
            evidence = load_candidate_evidence(session)
            # 3.8c: prefit ranks the unprofiled jobs for free; only the top
            # `max_profile_extractions` reach the paid extraction call, and the
            # run then scores every profiled job (scoring is free). A job whose
            # last answer was invalid is skipped (its marker stays).
            extracted, remaining = extract_top_prefit(
                settings=settings,
                evidence=evidence,
                max_extractions=load_defaults().max_profile_extractions,
                echo=None,
            )
            # extract_top_prefit commits through its own session; the Job
            # objects loaded here would carry a stale job_profile=None.
            session.expire_all()
            summary = match_jobs(
                settings=settings,
                evidence=evidence,
                output=params.get("output"),
                llm_notes=bool(params.get("llm_notes")),
                echo=None,
            )
            summary.profiles_extracted = extracted
            summary.unprofiled_remaining = remaining
        elif run.run_type == "profile_parse":
            summary = parse_profile_workflow(
                cv_text=params["cv_text"],
                description=params.get("description"),
                additional_experience=params.get("additional_experience"),
                echo=None,
            )
        elif run.run_type == "role_map":
            summary = map_roles_workflow(texts=list(params.get("texts") or []), echo=None)
        elif run.run_type == "careers_search":
            # M1 B2: the workflow owns its checkpoint per company and leaves the
            # run paused/cancelled itself; a pause here is never "completed".
            summary = run_careers_search(session, run)
        elif run.run_type == "name_collect":
            # M1 B3: same contract per source; the workflow builds its own
            # fetch client and authoring model.
            summary = run_name_collect(session, run)
        else:
            raise ValueError(f"Unsupported run type: {run.run_type}")
        session.refresh(run)
        if run.status == "cancelled_requested":
            finish_run(session, run, status="cancelled")
        elif run.status == "paused":
            # Paused mid-work: the run stays paused with no terminal state, so
            # a resume re-executes it. Never report "completed" over a pause.
            logger.info("Run %s paused; leaving it paused after its current step", run.id)
        else:
            finish_run(session, run, summary=summary)
    except Exception as exc:
        logger.exception("Run %s failed", run.id)
        try:
            session.rollback()
            session.refresh(run)
            if run.status == "paused":
                logger.info("Run %s paused during a failing step; keeping it paused", run.id)
                return
            finish_run(session, run, status="failed", error=f"{exc}\n{traceback.format_exc()[-4000:]}")
        except Exception:
            logger.exception("Could not persist terminal state for run %s", run.id)


class RunWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._queue: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-run-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            session = get_session()
            try:
                cancelled = session.scalar(
                    select(JobRun).where(JobRun.status == "cancelled_requested").order_by(JobRun.id).limit(1)
                )
                if cancelled is not None:
                    finish_run(session, cancelled, status="cancelled")
                    continue
                run = claim_next_run(session)
                if run is not None:
                    execute_run(session, run)
                    continue
            finally:
                session.close()
            self._wake.wait(timeout=1.0)
            self._wake.clear()
