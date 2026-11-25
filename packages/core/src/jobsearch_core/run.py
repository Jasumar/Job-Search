"""Workflow orchestration for the discover/collect/match phases (Phase 4.2a).

Service layer: callable from the CLI, the FastAPI backend (Phase 4.2b), or any
other non-Click context. All user-facing progress goes through the optional
``echo`` callback (defaults to ``click.echo``); rendering of the run summary
lives in the CLI layer, summary building here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import defer, selectinload, undefer

from jobsearch_core.config import (
    Settings,
    load_defaults,
    load_scope,
)
from jobsearch_core.cv_parse import map_custom_role, propose_evidence_from_cv, propose_profile_from_cv
from jobsearch_core.db.models import (
    Application,
    Company,
    CompanyRecipe,
    CompanyXhrCapture,
    Job,
    JobExclusion,
    JobRun,
    RecipeFlag,
    RunSource,
    SearchCache,
    UserEvidence,
    UserProfile,
)
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.llm.decisions import DecisionsClient
from jobsearch_core.log import get_logger
from jobsearch_core.profile import profile_snapshot, store_raw_cv
from jobsearch_core.profile_evidence import (
    CandidateEvidence,
    evidence_prompt_payload,
    load_candidate_evidence,
    store_additional_experience,
)
from jobsearch_core.report import generate_report
from jobsearch_core.scoring.match import (
    CONSIDER,
    RECOMMENDATION_ORDER,
    SKIP,
    WORTH,
    MatchResult,
    apply_result_to_job,
    explain_match,
    load_job_profile,
    match_config_from_defaults,
    results_to_report_data,
    score_job_against_evidence,
    select_jobs_for_notes,
)
from jobsearch_core.scoring.prefit import prefit_unprofiled
from jobsearch_core.scoring.profile import (
    InvalidProfileResponseError,
    extract_job_profile,
    populate_flattened_from_profile,
    profile_is_current,
)
from jobsearch_core.search.careers_search import AdaptivePacer, PacedSearchClient
from jobsearch_core.search.discover import DiscoveryRunSummary, discover_with_summary

logger = get_logger(__name__)

EchoFn = Callable[..., Any]


def _echo(echo: EchoFn | None, message: str) -> None:
    """Route user-facing progress: echo callback when given, else the logger."""
    if echo is not None:
        echo(message)
    else:
        logger.info(message)


@dataclass
class RunSummary:
    """Run-level outcome for a discovery or collection pass."""

    dry_run: bool
    mode: str = "discover"
    discovery_summary: DiscoveryRunSummary | None = None
    effective_defaults: dict[str, Any] = field(default_factory=dict)
    pending_review: int = 0
    accepted_sources: int = 0
    sources_scraped: int = 0
    promoted: int = 0
    jobs_discovered: int = 0
    jobs_with_descriptions: int = 0
    # A resolved sitemap entry is expanded into its posting pages; these keep
    # the slice visible (listed vs. scraped) instead of letting a truncated
    # read look like a complete one.
    sitemap_listed: int = 0
    sitemap_scraped: int = 0
    sitemap_truncated: int = 0
    jobs_scored: int = 0
    eligible_unscored: int = 0
    remaining_actions: list[str] = field(default_factory=list)
    # --- match phase (Phase 4.1) ---
    jobs_found: int = 0
    jobs_pending: int = 0
    profiles_extracted: int = 0
    unprofiled_remaining: int = 0
    notes_written: int = 0
    recommendations: dict[str, Any] = field(default_factory=dict)


def extract_missing_profiles(
    *,
    settings,
    jobs: list[Job],
    limit: int | None = None,
    retry_failed: bool = False,
    echo: EchoFn | None = None,
) -> int:
    """Extract a structured ``job_profile`` for jobs that lack one (Phase 3.6d).

    One bounded LLM call per job, prompt-hash cached. Backfills existing
    processed jobs and covers newly scraped ones; a job whose profile is
    current is never re-extracted. A profile from before MF3 (no ``job_role``)
    is not current and is extracted again. An invalid model answer marks the job
    (``profile_failed_at``) and the job is skipped until ``retry_failed`` clears
    the marker; any other failure (network, quota) leaves it retryable. Returns
    the number of profiles extracted.
    """
    candidates = [
        job
        for job in jobs
        if job.description
        and not profile_is_current(job.job_profile)
        and (retry_failed or job.profile_failed_at is None)
    ]
    ids = [job.id for job in candidates]
    if limit is not None:
        ids = ids[:limit]
    if not ids:
        return 0

    _echo(echo, f"Extracting job profiles for {len(ids)} job(s)...")
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    session = get_session()
    extracted = 0
    for index, candidate_id in enumerate(ids, 1):
        # Re-fetch within this session: callers may pass detached/stale instances.
        job = session.get(Job, candidate_id)
        if job is None or not job.description or profile_is_current(job.job_profile):
            continue
        if job.profile_failed_at is not None and not retry_failed:
            continue
        _echo(echo, f"[{index}/{len(ids)}] {job.raw_title} @ {job.raw_company}")
        try:
            profile = extract_job_profile(
                client, settings.llm_cache_dir, job.description, job.country
            )
            job.job_profile = json.dumps(profile, ensure_ascii=False)
            job.profile_failed_at = None
            populate_flattened_from_profile(job, profile)
            session.commit()
            extracted += 1
        except InvalidProfileResponseError as exc:
            session.rollback()
            _echo(echo, f"  invalid profile answer: {exc}")
            logger.warning("Invalid profile answer for job %s; marking it", job.id)
            job = session.get(Job, candidate_id)
            job.profile_failed_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
        except Exception as exc:
            session.rollback()
            _echo(echo, f"  failed: {exc}")
            logger.exception("Failed to extract profile for job %s", job.id)
    session.close()
    return extracted


def extract_top_prefit(
    *,
    settings,
    evidence: CandidateEvidence,
    max_extractions: int,
    echo: EchoFn | None = None,
) -> tuple[int, int]:
    """Extract profiles for the best prefit-ranked unprofiled jobs (3.8c M2).

    Prefit is free (``scoring/prefit.py``); only the top ``max_extractions`` jobs
    reach the paid extraction call, and each job is extracted once. Returns
    ``(extracted, remaining)``: ``remaining`` counts the jobs that still pass
    prefit and have no profile after this call (M4), so the next run takes them.
    """
    session = get_session()
    try:
        ranked = prefit_unprofiled(session, evidence).ranked
        top_ids = [entry.job_id for entry in ranked[:max_extractions]]
        jobs = [job for job in (session.get(Job, job_id) for job_id in top_ids) if job]
        session.expunge_all()
    finally:
        session.close()
    extracted = extract_missing_profiles(
        settings=settings, jobs=jobs, limit=max_extractions, echo=echo
    )
    return extracted, count_unprofiled_remaining(evidence)


def count_unprofiled_remaining(evidence: CandidateEvidence) -> int:
    """Jobs that pass prefit and still have no profile (the M4 count)."""
    session = get_session()
    try:
        return len(prefit_unprofiled(session, evidence).ranked)
    finally:
        session.close()


def match_status() -> dict[str, Any]:
    """What the results screen shows beside the list (3.8c M3, M4).

    ``profile_changed`` is true when the profile or the evidence was saved after
    the last completed match run started (that run read the older information);
    with no finished run there is nothing to be out of date against, so it is
    false. MF5: ``results_current`` is true only when a match run completed and
    no save came after its start; Matches shows nothing until then.
    ``unprofiled_remaining`` is the M4 count. ``evidence_ready`` is MF1's
    gate: scoring needs saved skills or experience.
    """
    defaults = load_defaults()
    session = get_session()
    try:
        evidence = load_candidate_evidence(session)
        edited = [
            value
            for (value,) in (
                session.query(UserProfile.updated_at).all()
                + session.query(UserEvidence.updated_at).all()
            )
            if value is not None
        ]
        last_match = (
            session.query(JobRun.finished_at, JobRun.started_at)
            .filter(JobRun.run_type == "match", JobRun.status == "completed")
            .order_by(JobRun.finished_at.desc())
            .first()
        )
        last_finished = last_match[0] if last_match else None
        last_started = (last_match[1] or last_match[0]) if last_match else None
        profile_updated = max(edited) if edited else None
        remaining = len(prefit_unprofiled(session, evidence).ranked)
    finally:
        session.close()
    return {
        "profile_updated_at": profile_updated,
        "last_match_finished_at": last_finished,
        "profile_changed": bool(
            last_started and profile_updated and profile_updated > last_started
        ),
        "results_current": bool(
            last_started and not (profile_updated and profile_updated > last_started)
        ),
        "evidence_ready": evidence.can_score,
        "unprofiled_remaining": remaining,
        "worth_threshold": defaults.worth_threshold,
        "consider_threshold": defaults.consider_threshold,
    }


def check_link_liveness(
    *,
    settings,
    jobs: list[Job],
    max_checks: int = 10,
    echo: EchoFn | None = None,
) -> int:
    """Bounded HEAD (fallback GET) liveness check on job URLs (Phase 3.6d).

    Only checks up to ``max_checks`` job URLs that have never been checked;
    failures mark the job (``link_ok=False``) rather than blocking the run.
    Returns the number of links checked.
    """
    from jobsearch_core.ingest.http import PoliteClient

    candidates = [
        job for job in jobs if job.url and job.link_checked_at is None
    ]
    ids = [job.id for job in candidates][:max_checks]
    if not ids:
        return 0

    _echo(echo, f"Checking link liveness for {len(ids)} job(s)...")
    http_client = PoliteClient(max_retries=1, min_interval=0.5)
    checked = 0
    session = get_session()
    now = datetime.now(UTC).replace(tzinfo=None)
    for candidate_id in ids:
        # Re-fetch within this session: callers may pass detached/stale instances;
        # skip jobs that got checked in the meantime.
        job = session.get(Job, candidate_id)
        if job is None or job.link_checked_at is not None:
            continue
        ok = False
        try:
            http_client._wait_for_host(urlparse(job.url).netloc)
            response = http_client._session.request(
                "HEAD", job.url, timeout=http_client._timeout
            )
            if response.status_code in (405, 501):
                # HEAD not supported; fall back to GET.
                response = http_client._session.request(
                    "GET", job.url, timeout=http_client._timeout
                )
            ok = response.status_code < 400
        except Exception as exc:
            logger.warning("Liveness check failed for %s: %s", job.url, exc)
        job.link_checked_at = now
        job.link_ok = ok
        session.commit()
        checked += 1
    http_client.close()
    session.close()
    return checked


def match_jobs(
    *,
    settings: Settings,
    evidence,
    output: str | None = None,
    limit: int | None = None,
    scope: str = "tech",
    max_liveness_checks: int | None = None,
    llm_notes: bool = False,
    match_config=None,
    echo: EchoFn | None = None,
) -> RunSummary:
    """Phase 4.1: rank collected jobs against the persisted evidence.

    Deterministic and offline by default: every job that already has a
    structured ``job_profile`` is scored from that profile and the DB evidence.
    No LLM call is made unless ``llm_notes`` is set, and even then the notes
    pass is bounded by ``max_llm_notes`` and prompt-cached.

    Scoring is free, so every run scores every profiled job again (3.8c M3). A
    profile or evidence edit still changes nothing until the owner starts a run.

    Jobs with no structured profile are left untouched and reported as pending:
    they are not scored, so they are never presented as rejected recommendations.
    Applying remains the separate ``tailor`` workflow behind its approval gate.
    """
    session = get_session()
    defaults = load_defaults()
    match_config = match_config or match_config_from_defaults(defaults)
    summary = RunSummary(dry_run=False, mode="match")

    # RF9: the large text columns stay unloaded for the whole table. Eligibility
    # reads only whether a description exists; the candidates' own text is
    # loaded in one batch below.
    jobs = session.query(Job).options(*_defer_job_bulk()).all()
    has_description = _jobs_with_description(session)
    summary.jobs_found = len(jobs)
    if not jobs:
        _echo(echo, "No jobs in database.")
        # Always expose all three groups so callers (CLI echo, API summary)
        # never have to special-case an empty run.
        summary.recommendations = {name: [] for name in RECOMMENDATION_ORDER}
        summary.effective_defaults = {
            "llm_notes": llm_notes,
            "max_llm_notes": match_config.max_llm_notes,
        }
        session.close()
        return summary

    # Phase 3.6d: a bounded liveness check on job URLs. This is network I/O,
    # not scoring: it never changes a score, only link_ok.
    #
    # Profile *extraction* is deliberately NOT done here. Matching is defined
    # as offline-deterministic over persisted profiles, so a job without one is
    # `pending` (invariant 4), not something to fix with a paid LLM call
    # mid-match. Backfill remains available as the explicit
    # `jobsearch_core scrape` step, which owns it.
    if max_liveness_checks is None:
        max_liveness_checks = defaults.max_liveness_checks
    check_link_liveness(settings=settings, jobs=jobs, max_checks=max_liveness_checks, echo=echo)

    candidates = [job for job in jobs if job.id in has_description]
    if limit:
        candidates = candidates[:limit]
    _load_candidate_text(session, candidates)

    _echo(echo, f"Processing {len(candidates)} of {len(jobs)} jobs ({len(candidates)} eligible in this run)...")

    results: list[MatchResult] = []
    results_jobs: list[Job] = []
    pending: list[Job] = []
    for index, job in enumerate(candidates, 1):
        profile = load_job_profile(job)
        if profile is None:
            pending.append(job)
            _echo(echo, f"[{index}/{len(candidates)}] {job.raw_title} @ {job.raw_company}")
            _echo(echo, "  no structured job profile; left unscored (pending)")
            continue
        _echo(echo, f"[{index}/{len(candidates)}] {job.raw_title} @ {job.raw_company}")
        result = score_job_against_evidence(job, profile, evidence, match_config)
        apply_result_to_job(job, result)
        results.append(result)
        results_jobs.append(job)
        _echo(echo, f"  {result.recommendation} {result.score:.2%}")
        summary.jobs_scored += 1

    session.commit()

    notes_written = 0
    if llm_notes and match_config.max_llm_notes > 0:
        notes_written = _apply_llm_notes(
            session=session,
            settings=settings,
            results=results,
            evidence=evidence,
            budget=match_config.max_llm_notes,
            echo=echo,
        )
    summary.notes_written = notes_written

    summary.recommendations = results_to_report_data(
        results, {job.id: job for job in results_jobs}
    )
    summary.jobs_pending = len(pending)
    _write_report(session, output, summary.recommendations)

    grouped = summary.recommendations
    _echo(
        echo,
        f"Worth applying: {len(grouped[WORTH])} | "
        f"Consider: {len(grouped[CONSIDER])} | "
        f"Skip: {len(grouped[SKIP])} | "
        f"Pending (no job profile): {summary.jobs_pending}",
    )
    if output:
        _echo(echo, f"Report written to {output}")
    session.close()
    return summary


def match_one_job(*, job_id: int, evidence, match_config=None, echo: EchoFn | None = None) -> MatchResult | None:
    """Match a single job on demand (the ``score-job`` CLI command).

    Returns ``None`` when the job does not exist or has no description. A job
    with no structured profile is reported as unscorable rather than scored on
    description text, because Phase 4.1 matches profiles, not raw text.
    """
    session = get_session()
    match_config = match_config or match_config_from_defaults(load_defaults())
    job = session.get(Job, job_id)
    if job is None or not job.description:
        session.close()
        return None
    profile = load_job_profile(job)
    if profile is None:
        session.close()
        return None
    result = score_job_against_evidence(job, profile, evidence, match_config)
    apply_result_to_job(job, result)
    session.commit()
    session.close()
    return result


def _apply_llm_notes(
    *,
    session,
    settings: Settings,
    results: list[MatchResult],
    evidence,
    budget: int,
    echo: EchoFn | None = None,
) -> int:
    """Add cached LLM narrative to the top ``budget`` non-skipped results.

    Bounded and non-blocking on the deterministic outcome: a failure here leaves
    the deterministic strengths/gaps/blockers in place and the run succeeds,
    because the notes are an explanation, not an input to the recommendation.
    """
    targets = select_jobs_for_notes(results)[:budget]
    if not targets:
        return 0
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    written = 0
    for result in targets:
        job = session.get(Job, result.job_id)
        profile = load_job_profile(job) if job is not None else None
        if job is None or profile is None:
            continue
        try:
            notes = explain_match(
                client,
                settings.llm_cache_dir,
                profile,
                evidence_prompt_payload(evidence),
            )
        except Exception as exc:
            session.rollback()
            _echo(echo, f"  notes failed for job {result.job_id}: {exc}")
            logger.exception("LLM notes failed for job %s", result.job_id)
            continue
        # The recommendation group is a deterministic outcome, decided before
        # this pass ran. The notes are an explanation of it, never a revision:
        # narrative fields may be replaced, but blockers stay deterministic.
        # This matters because `report.classify_for_report` re-derives the group
        # from persisted (score, has_blockers) alone — a report regenerated later
        # without this run's in-memory results. Letting the model invent a
        # blocker would silently demote a Worth job to Skip in that report,
        # contradicting the recommendation this run already reported.
        strengths = list(notes.get("strengths", [])) or result.strengths
        gaps = list(notes.get("gaps", [])) or result.gaps
        summary_text = notes.get("summary", "") or result.summary
        job.fit_strengths = json.dumps(strengths, ensure_ascii=False)
        job.fit_gaps = json.dumps(gaps, ensure_ascii=False)
        job.fit_summary = summary_text
        job.fit_blockers = json.dumps(result.blockers, ensure_ascii=False)
        result.strengths = strengths
        result.gaps = gaps
        result.summary = summary_text
        written += 1
    session.commit()
    return written


def _write_report(session, output: str | None, recommendations: dict | None = None) -> None:
    """Regenerate the report from persisted data only."""
    if not output:
        return
    generate_report(
        session.query(Job).options(*_defer_job_bulk()).all(),
        output_path=output,
        applications=session.query(Application).all(),
        recommendations=recommendations,
    )


def run_workflow(
    *,
    dry_run: bool = False,
    country: str | None = None,
    role: str | None = None,
    search_backend: str | None = None,
    max_baseline_queries: int = 8,
    max_companies: int = 10,
    expand_queries: bool = False,
    max_variants_per_baseline: int = 2,
    mine_listicles: bool = True,
    max_listicles: int = 3,
    max_companies_per_listicle: int = 15,
    scope: str = "tech",
    effective_defaults: dict[str, Any] | None = None,
    echo: EchoFn | None = None,
) -> RunSummary:
    """Run the discovery-and-confirm pass: find employer identities, persist new
    candidates to the review queue, and stop at the confirmation gate.

    A discovery pass never scrapes confirmed positions; that is the separate
    confirmed-source collection step (``collect_workflow`` / ``jobsearch_core
    collect``). Its remaining manual action is ``jobsearch_core discover review``.
    """
    # The CLI and the worker resolve this from config/defaults.yaml before the
    # call; a direct caller gets the approved default instead of a stale ddgs.
    search_backend = search_backend or load_defaults().search_backend
    settings = Settings()
    init_db(settings.database_url)
    summary = RunSummary(
        dry_run=dry_run,
        mode="discover",
        effective_defaults=effective_defaults or {},
    )
    resolved_scope = load_scope(scope)

    preferences: dict[str, Any] | None = None
    profile_reference: str | None = None
    # Phase 3.8f read timing (decided 2026-09-11): the profile is snapshotted
    # once here (run claim/execution start) and used for the whole run —
    # mid-run profile edits never affect an in-flight run. Phase 4.1a: the DB
    # is the only source — ``data/evidence.yaml`` no longer exists.
    session = get_session()
    try:
        profile_snap = profile_snapshot(session)
        profile_reference = profile_snap.reference
    finally:
        session.close()
    if profile_snap.countries or profile_snap.preferences:
        preferences = {
            "target_countries": profile_snap.countries,
            "job_preferences": profile_snap.preferences,
            "source": "db_profile",
        }
        _echo(
            echo,
            f"Profile snapshot used for discovery (updated {profile_reference}): "
            f"{len(profile_snap.countries)} countr(ies), {len(profile_snap.preferences)} preference(s).",
        )
    else:
        logger.warning(
            "No profile countries/preferences stored; discovery baseline countries "
            "fall back to all scope countries."
        )
    summary.effective_defaults["profile_reference"] = profile_reference

    # Phase 3.8f wizard selection (decided 2026-09-11): the profile's
    # role-family chips map to segment templates via the ordered
    # role→segment mapping in the scope. Ordered intersection semantics —
    # a segment is kept only if every chosen family maps to it, per-chip
    # cap 3, mapping order decides. No profile selection = all templates
    # (pre-wizard narrowing rule).
    profile_segments: list[str] | None = None
    if profile_snap.role_families:
        per_family: list[list[str]] = []
        for family_id in profile_snap.role_families:
            mapping = resolved_scope.role_family_segments.get(family_id)
            if mapping is None:
                logger.warning(
                    "Profile role family %r has no segment mapping in the %s "
                    "scope; ignoring it for template selection.",
                    family_id,
                    scope,
                )
                continue
            per_family.append(mapping.segments[:3])
        if per_family:
            shared = set(per_family[0]).intersection(*(set(s) for s in per_family[1:]))
            # Ordered by the mapping order of the first chosen family, so the
            # resulting template priority is deterministic and data-driven.
            profile_segments = [s for s in per_family[0] if s in shared]
            summary.effective_defaults["profile_segments"] = profile_segments

    if search_backend == "searxng":
        from jobsearch_core.search.client import SearXNGSearchClient, _ensure_searxng
        search_client = SearXNGSearchClient()
        try:
            _ensure_searxng(search_client)
        except RuntimeError as exc:
            # Non-Click safe: raise a normal exception; the CLI layer turns it
            # into a user-facing error and exit code (Phase 4.2a).
            raise RuntimeError(f"Search backend unavailable: {exc}") from exc
    else:
        from jobsearch_core.search.client import build_search_client
        search_client = build_search_client(search_backend, api_key=Settings().brave_search_key)

    # Discovery used to fire every baseline and follow-up query back to back;
    # the pacer spaces them (human-scale for a free engine, one request per
    # second for the paid one). careers_search applies the same rule.
    search_client = PacedSearchClient(search_client, AdaptivePacer.for_backend(search_backend))

    from jobsearch_core.ingest.http import PoliteClient

    http_client = PoliteClient()
    llm_client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    gate_client: DecisionsClient | None = None
    if settings.openrouter_api_key:
        gate_client = DecisionsClient(
            api_key=settings.openrouter_api_key,
            model=settings.typesafe_model,
            max_retries=settings.llm_max_retries,
        )
    else:
        logger.warning("No OpenRouter API key; listicle chunk gating disabled")
    try:
        candidates, discovery_summary = discover_with_summary(
            search_client,
            country,
            role,
            dry_run=dry_run,
            http_client=http_client,
            llm_client=llm_client,
            gate_client=gate_client,
            cache_dir=settings.llm_cache_dir,
            identity_first=True,
            max_baseline_queries=max_baseline_queries,
            max_companies=max_companies,
            expand_queries=expand_queries,
            max_variants_per_baseline=max_variants_per_baseline,
            preferences=preferences,
            segments=profile_segments,
            mine_listicles=mine_listicles,
            max_listicles=max_listicles,
            max_companies_per_listicle=max_companies_per_listicle,
            scope=resolved_scope,
        )
    finally:
        http_client.close()
        if hasattr(search_client, "close"):
            search_client.close()

    summary.discovery_summary = discovery_summary
    _echo(
        echo,
        f"Discovery: {discovery_summary.search_requests_used} requests used, "
        f"{discovery_summary.known_companies} known companies suppressed, "
        f"{discovery_summary.known_alternate_urls} alternate hiring URLs for known companies, "
        f"{discovery_summary.new_companies} new company candidates.",
    )
    if discovery_summary.search_failures:
        _echo(
            echo,
            f"Search failures: {discovery_summary.search_failures} query/queries produced no "
            f"answer (blocked or throttled engine) — those sweeps are incomplete, not empty.",
        )
    if discovery_summary.listicle_fetches:
        _echo(
            echo,
            f"Listicles: {discovery_summary.listicle_fetches} page(s) mined, "
            f"{discovery_summary.listicle_companies} company(ies) mined from them.",
        )
    if discovery_summary.listicle_renders:
        _echo(
            echo,
            f"Client-rendered listicles: {discovery_summary.listicle_renders} page(s) re-fetched "
            f"through a browser (company names were absent from the HTML).",
        )
    if discovery_summary.hub_routed or discovery_summary.noise_dropped:
        _echo(
            echo,
            f"Routing: {discovery_summary.hub_routed} hub signal(s) routed to mining, "
            f"{discovery_summary.noise_dropped} noise signal(s) dropped.",
        )
    if candidates:
        _echo(echo, f"{len(candidates)} new candidate(s) from discovery.")
    if discovery_summary.candidates_persisted:
        _echo(echo, f"{discovery_summary.candidates_persisted} candidate(s) persisted to the review queue.")

    session = get_session()
    summary.pending_review = (
        session.query(Company)
        .filter(Company.status == "pending")
        .count()
    )
    summary.accepted_sources = (
        session.query(Company)
        .filter(Company.status == "accepted")
        .count()
    )
    session.close()

    _echo(echo, "\nDiscovery pass complete; confirmed sources are not scraped in this pass.")
    if summary.pending_review:
        summary.remaining_actions.append(
            f"review the {summary.pending_review} pending candidate(s) next: "
            "run `jobsearch_core discover review --interactive`"
        )
    elif summary.accepted_sources:
        summary.remaining_actions.append(
            "confirmed sources are ready to scrape: run `jobsearch_core collect`"
        )

    return summary


def collect_workflow(
    *,
    dry_run: bool = False,
    max_sources: int | None = None,
    limit: int | None = None,
    no_details: bool = False,
    output: str = "reports/report.md",
    batch: int | None = None,
    refresh_known: bool = False,
    company_ids: list[int] | None = None,
    scope: str = "tech",
    echo: EchoFn | None = None,
    run_id: int | None = None,
) -> RunSummary:
    """Collect from confirmed sources: scrape accepted (and optionally known)
    ``companies`` rows and promote accepted rows to ``known``. Pending discovery
    remains a manual review step.

    No scoring: this workflow gathers postings and never ranks them. Profile
    extraction, ``total_score`` and the report belong to a match run, so a
    search run cannot present a ranked list the user did not ask for.

    ``run_id`` is the worker's run: it gives each company a ``run_sources`` row
    and lets a verified recipe be replayed instead of the generic scraper
    (R5a). The CLI path passes none, so it neither writes a manifest nor
    replays."""
    settings = Settings()
    init_db(settings.database_url)
    summary = RunSummary(dry_run=dry_run, mode="collect")

    session = get_session()
    summary.pending_review = (
        session.query(Company).filter(Company.status == "pending").count()
    )
    summary.accepted_sources = (
        session.query(Company).filter(Company.status == "accepted").count()
    )
    session.close()

    from jobsearch_core.ingest.scrapers.pipeline import scrape_company_entries

    # The gate is the only LLM call a replay may make, and only on its cadence
    # (R3). No key means no gate: the replay still records its verified set.
    gate_llm = None
    if run_id is not None and settings.openrouter_api_key:
        gate_llm = LLMClient(
            api_key=settings.openrouter_api_key,
            model=settings.analysis_model,
            base_url=settings.llm_base_url,
            min_interval_seconds=settings.llm_min_interval_seconds,
            max_retries=settings.llm_max_retries,
        )

    _echo(echo, "\nScraping accepted and verified company sources...")
    stats = scrape_company_entries(
        dry_run=dry_run,
        fetch_descriptions=not no_details,
        accepted_batch=batch,
        refresh_known=refresh_known,
        company_ids=company_ids,
        limit=max_sources,
        run_id=run_id,
        llm=gate_llm,
    )

    summary.sources_scraped = stats.get("companies", 0)
    summary.promoted = stats.get("promoted", 0)
    summary.sitemap_listed = stats.get("sitemap_listed", 0)
    summary.sitemap_scraped = stats.get("sitemap_scraped", 0)
    summary.sitemap_truncated = stats.get("sitemap_truncated", 0)
    summary.jobs_discovered = (
        stats.get("total_added", 0)
        + stats.get("total_dup", 0)
        + stats.get("total_no_desc", 0)
    )
    summary.jobs_with_descriptions = stats.get("total_added", 0)
    _echo(
        echo,
        f"Scraped {summary.sources_scraped} source(s): {summary.jobs_discovered} jobs seen, "
        f"{summary.jobs_with_descriptions} with descriptions.",
    )
    if summary.promoted:
        _echo(echo, f"Promoted {summary.promoted} accepted source(s) to known.")
    if summary.sitemap_listed:
        truncation = "; truncated at the page ceiling" if summary.sitemap_truncated else ""
        _echo(
            echo,
            f"Sitemap listings: {summary.sitemap_scraped} of {summary.sitemap_listed} "
            f"posting URLs scraped{truncation}.",
        )

    # Job search gathers postings; it does not judge them. Scoring, job-profile
    # extraction and the report write moved out of this workflow (owner
    # decision): a collect run that scored ranked every posting against the
    # profile with no match context, and paid for an LLM profile extraction the
    # user never asked for. A match run owns all three.
    session = get_session()
    summary.eligible_unscored = (
        session.query(Job)
        .filter(Job.description.isnot(None), Job.total_score.is_(None))
        .count()
    )
    session.close()

    if summary.pending_review:
        summary.remaining_actions.append(
            f"{summary.pending_review} discovery candidate(s) await review: "
            "run `jobsearch_core discover review --interactive`"
        )
    if not dry_run and summary.eligible_unscored:
        summary.remaining_actions.append(
            f"{summary.eligible_unscored} collected job(s) are unscored: run a match to rank them"
        )
    if not dry_run and not summary.accepted_sources and summary.jobs_discovered == 0:
        summary.remaining_actions.append(
            "No accepted discovery sources and no new jobs found; accept candidates via review to scrape them."
        )

    return summary


def parse_profile_workflow(
    *,
    cv_text: str,
    description: str | None = None,
    additional_experience: str | None = None,
    echo: EchoFn | None = None,
) -> dict[str, Any]:
    """``profile_parse`` run (Phase 3.8f): persist the raw CV, then propose.

    Ordering is the invariant: the raw CV text is persisted at parse time
    (before the LLM call) so it is never lost; the proposal is returned for
    the run ``summary`` and is never written to ``user_profile``.

    Phase 4.1a: ``additional_experience`` is the one field this run DOES
    persist, because it is user-authored rather than LLM output. It is stored
    verbatim and is deliberately excluded from both LLM calls, so user prose
    can never be parsed, normalized, or lost (invariant b).
    """
    settings = Settings()
    init_db(settings.database_url)
    session = get_session()
    try:
        profile = store_raw_cv(session, cv_text=cv_text, description=description)
        if additional_experience is not None:
            store_additional_experience(session, additional_experience)
        reference = profile.updated_at.isoformat() if isinstance(profile.updated_at, datetime) else None
    finally:
        session.close()

    _echo(echo, "Parsing CV into proposed profile fields...")
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    proposal = propose_profile_from_cv(client, settings.llm_cache_dir, cv_text, description)
    # Phase 4.1a step 3: the same cached response also yields a proposed
    # ``user_evidence`` document. Proposal only — like the profile proposal
    # it is never auto-saved (invariant c).
    evidence_proposal = propose_evidence_from_cv(
        client, settings.llm_cache_dir, cv_text, description
    )
    _echo(echo, f"Proposed {len(proposal.get('skills', []))} skill(s); proposal awaits user confirmation.")
    # Flat mirrors of the proposal for the wizard (3.8g/3.8h live-validation
    # fix): the wizard reads summary.suggested_role_families and
    # summary.extracted_cv_text directly, without unwrapping `proposal`.
    return {
        "proposal": proposal,
        "evidence_proposal": evidence_proposal,
        "profile_reference": reference,
        "suggested_role_families": proposal.get("suggested_role_families") or [],
        "extracted_cv_text": cv_text,
        "note": "Proposal only — never auto-saved; Save writes PUT /profile and PUT /profile/evidence.",
    }


def map_roles_workflow(*, texts: list[str], echo: EchoFn | None = None) -> dict[str, Any]:
    """MF3: map typed roles to taxonomy ids (the ``role_map`` run).

    One prompt-cached LLM call per role text. The result is a proposal in the
    run summary; the owner edits it and Save stores it with the profile.
    """
    settings = Settings()
    client = LLMClient(
        api_key=settings.openrouter_api_key,
        model=settings.analysis_model,
        base_url=settings.llm_base_url,
        min_interval_seconds=settings.llm_min_interval_seconds,
        max_retries=settings.llm_max_retries,
    )
    mapped: list[dict[str, Any]] = []
    for text in texts:
        ids = map_custom_role(client, settings.llm_cache_dir, text)
        _echo(echo, f"{text}: {', '.join(ids) or 'no match'}")
        mapped.append({"text": text, "ids": ids})
    return {"custom_roles": mapped, "note": "Proposal only — saved with the profile."}


def _defer_job_bulk() -> list:
    """Loader options that skip the large ``jobs`` text columns.

    Readers that need one of them touch the attribute, which loads it on demand
    for that row, or call :func:`_load_descriptions` for a batch.
    """
    return [defer(Job.description), defer(Job.job_profile), defer(Job.extracted_requirements)]


def _load_candidate_text(session, jobs: list[Job]) -> None:
    """Fill the deferred columns of ``jobs`` with one query.

    The query selects the columns with ``undefer`` on instances the session
    already holds; it fills only attributes that are still unloaded.
    """
    if not jobs:
        return
    session.query(Job).options(
        undefer(Job.description), undefer(Job.job_profile)
    ).filter(Job.id.in_([job.id for job in jobs])).all()


def _jobs_with_description(session) -> set[int]:
    """Ids of jobs whose description is non-empty, without loading any text."""
    rows = session.query(Job.id).filter(Job.description.isnot(None), Job.description != "")
    return {row_id for (row_id,) in rows}


def list_jobs(
    *,
    status: str | None = None,
    country: str | None = None,
    min_score: float | None = None,
    scored: bool | None = None,
    tailored: bool | None = None,
    company_id: int | None = None,
    unattributed: bool = False,
    order: str = "id",
    limit: int | None = None,
) -> list[Job]:
    """Query jobs with the ``jobs list`` filters (Phase 4.2a service extraction).

    The CLI renders the table; the API (Phase 4.2b) serializes the returned
    ORM objects. Returns detached-safe live instances from a fresh session.

    ``company_id`` is the gap 12 foreign key; ``unattributed`` selects the jobs
    that have no owning company, which is how a scrape that could not be
    attributed becomes visible instead of silently blending into a job list.
    """
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    # selectinload: the CLI renders the `applications` (tailored) flag on
    # detached instances after the session closes (Phase 4.2a).
    # RF9: the list never reads the large text columns, so they are deferred;
    # every filter and the limit run in SQL, not on a fully loaded table.
    query = session.query(Job).options(
        selectinload(Job.applications), *_defer_job_bulk()
    )

    if status:
        query = query.filter(Job.status == status)
    if country:
        query = query.filter(Job.country == country)
    if min_score is not None:
        query = query.filter(Job.total_score >= min_score)
    if company_id is not None:
        query = query.filter(Job.company_id == company_id)
    elif unattributed:
        query = query.filter(Job.company_id.is_(None))
    if scored is True:
        query = query.filter(Job.total_score.isnot(None))
    elif scored is False:
        query = query.filter(Job.total_score.is_(None))

    if tailored is True:
        query = query.filter(Job.applications.any())
    elif tailored is False:
        query = query.filter(~Job.applications.any())

    if order == "score_desc":
        query = query.order_by(Job.total_score.desc(), Job.id)
    else:
        query = query.order_by(Job.id)
    if limit is not None:
        query = query.limit(limit)
    jobs = query.all()

    session.expunge_all()
    session.close()
    return jobs


def delete_job(job_id: int) -> bool:
    """Delete a posting a human judged not to be a job (owner request).

    The row is deleted, but its ``dedup_hash`` is first written to
    ``job_exclusions``: duplicate detection reads that column from ``jobs``, so
    without the tombstone the next scrape would re-add the same posting and the
    decision would have to be made again. Returns False when no such job exists.
    """
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return False
        if job.dedup_hash:
            known = (
                session.query(JobExclusion)
                .filter(JobExclusion.dedup_hash == job.dedup_hash)
                .first()
            )
            if known is None:
                session.add(
                    JobExclusion(
                        dedup_hash=job.dedup_hash,
                        url=job.url,
                        raw_title=job.raw_title,
                    )
                )
        session.delete(job)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass
class CompanyDeleteReport:
    """Outcome (or dry-run preview) of :func:`delete_companies`.

    ``deleted`` and ``blocked`` hold ``{"id", "name", "jobs"}`` dicts (blocked
    ones also carry ``reason``); ``unknown_ids`` are ids that no longer exist.
    """

    deleted: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    unknown_ids: list[int] = field(default_factory=list)


_LIVE_RUN_SOURCE_STATUSES = ("queued", "running", "paused")


def delete_companies(company_ids: list[int], *, dry_run: bool = False) -> CompanyDeleteReport:
    """Delete companies and every job linked to them (owner request).

    Rules (owner-approved 2026-11-23):

    * Each deleted job's ``dedup_hash`` goes to ``job_exclusions``, so a later
      scrape does not bring the posting back (same rule as :func:`delete_job`).
    * Recipes, XHR captures and the search cache of the company are deleted.
      ``run_sources`` rows of the company are deleted too: the owner CHECK
      forbids a NULL ``company_id`` there. ``recipe_flags`` stay, with their
      provenance pointer set to NULL.
    * A company is blocked, not deleted, when one of its jobs has an
      application (tailored work) or a run still works on it.

    Each company is its own transaction, so one blocked company never stops the
    others. ``dry_run`` reports what would happen and writes nothing.
    """
    report = CompanyDeleteReport()
    session = get_session()
    try:
        for company_id in dict.fromkeys(company_ids):
            company = session.get(Company, company_id)
            if company is None:
                report.unknown_ids.append(company_id)
                continue
            jobs = session.query(Job).filter(Job.company_id == company_id).all()
            info: dict[str, Any] = {"id": company.id, "name": company.name, "jobs": len(jobs)}
            if any(job.applications for job in jobs):
                report.blocked.append({**info, "reason": "a job has an application"})
                continue
            live = (
                session.query(RunSource)
                .filter(
                    RunSource.company_id == company_id,
                    RunSource.status.in_(_LIVE_RUN_SOURCE_STATUSES),
                )
                .count()
            )
            if live:
                report.blocked.append({**info, "reason": "a run is still working on it"})
                continue
            report.deleted.append(info)
            if dry_run:
                continue

            recipe_ids = [
                rid
                for (rid,) in session.query(CompanyRecipe.id).filter(
                    CompanyRecipe.company_id == company_id
                )
            ]
            known = {
                h for (h,) in session.query(JobExclusion.dedup_hash).all() if h
            }
            for job in jobs:
                if job.dedup_hash and job.dedup_hash not in known:
                    known.add(job.dedup_hash)
                    session.add(
                        JobExclusion(
                            dedup_hash=job.dedup_hash, url=job.url, raw_title=job.raw_title
                        )
                    )
                session.delete(job)
            session.query(RunSource).filter(RunSource.company_id == company_id).delete()
            if recipe_ids:
                session.query(RunSource).filter(RunSource.recipe_id.in_(recipe_ids)).update(
                    {RunSource.recipe_id: None}, synchronize_session=False
                )
                session.query(RecipeFlag).filter(RecipeFlag.recipe_id.in_(recipe_ids)).update(
                    {RecipeFlag.recipe_id: None}, synchronize_session=False
                )
            session.query(CompanyRecipe).filter(CompanyRecipe.company_id == company_id).delete()
            session.query(CompanyXhrCapture).filter(
                CompanyXhrCapture.company_id == company_id
            ).delete()
            session.query(SearchCache).filter(SearchCache.company_id == company_id).delete()
            session.delete(company)
            session.commit()
        return report
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def print_effective_defaults(effective: dict[str, Any]) -> None:
    """Print the effective per-run settings resolved from flags + defaults."""
    if not effective:
        return
    pairs = " ".join(f"{key}={value}" for key, value in sorted(effective.items()))
    import click

    click.echo(f"Effective settings: {pairs}")


def print_run_summary(summary: RunSummary) -> None:
    """Render the run summary and remaining manual actions."""
    import click

    click.echo("\n===== RUN SUMMARY =====")
    click.echo(f"Dry-run: {summary.dry_run}")
    print_effective_defaults(summary.effective_defaults)
    if summary.mode == "collect":
        click.echo(
            f"Scrape: {summary.sources_scraped} sources, {summary.jobs_discovered} jobs discovered, "
            f"{summary.jobs_with_descriptions} with descriptions."
        )
        if summary.promoted:
            click.echo(f"Promotion: {summary.promoted} accepted source(s) promoted to known.")
        click.echo(f"Scoring: {summary.jobs_scored} scored, {summary.eligible_unscored} eligible unscored remain.")
    else:
        if summary.discovery_summary:
            d = summary.discovery_summary
            click.echo(
                f"Discovery: {d.identity_signals} identity signals, {d.new_companies} new companies, "
                f"{d.known_companies} known, {d.known_alternate_urls} alternate URLs, "
                f"{d.sources_selected} hiring sources selected, {d.search_requests_used} requests used."
            )
            if d.search_failures:
                click.echo(
                    f"Search failures: {d.search_failures} query/queries produced no answer "
                    f"(blocked or throttled engine) — the sweep is incomplete, not empty."
                )
            if d.listicle_fetches:
                click.echo(
                    f"Listicles: {d.listicle_fetches} page(s) mined, "
                    f"{d.listicle_companies} company(ies) mined from them."
                )
            if d.listicle_renders:
                click.echo(
                    f"Client-rendered listicles: {d.listicle_renders} page(s) re-fetched "
                    f"through a browser."
                )
            if d.hub_routed or d.noise_dropped:
                click.echo(
                    f"Routing: {d.hub_routed} hub signal(s) routed, {d.noise_dropped} noise dropped."
                )
        click.echo(
            f"Review queue: {summary.pending_review} pending, {summary.accepted_sources} accepted sources."
        )
    if summary.remaining_actions:
        click.echo("\nRemaining manual actions:")
        for action in summary.remaining_actions:
            click.echo(f"  - {action}")
    else:
        click.echo("\nNo remaining manual actions.")
