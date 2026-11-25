"""Production replay of a stored recipe (R5a, plan-scraping.md §Recipe closure).

The benchmark harness (``benchmarks/recipe_author/run_replay.py``) proved the
contract; this module is the production call site. It replays a verified recipe
against the live page, replaces the run source's verified URL set through
``record_verified_urls``, and feeds the outcome to R3's verifier: statistics and
signals on every replay, the posting-existence gate on its cadence, and the stop
rule.

A replay that raises is an error, not an empty board: the caller marks the
recipe stale and falls back to the generic scraper. Evidence is written only
for a replay that produced postings (or an empty set it actually observed), so
a failed fetch never looks like every posting disappeared.

Writes only through the caller's Session and flushes; the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobsearch_core.db.models import CompanyRecipe
from jobsearch_core.db.recipe_flags import has_prior_none, latest_flag, record_flag
from jobsearch_core.db.source_evidence import baseline_url_count, record_verified_urls
from jobsearch_core.ingest.llm_protocol import JsonCompleter
from jobsearch_core.ingest.recipe import (
    ApiRecipe,
    DomRecipe,
    RecipeError,
    parse_body,
    run_api_recipe_report,
    run_dom_recipe_report,
)
from jobsearch_core.ingest.recipe_verify import (
    DomAgreement,
    GateError,
    GateVerdict,
    ReplayStatistics,
    Signal,
    body_hash,
    collect_statistics,
    dom_agreement,
    run_posting_gate,
    should_run_gate,
    signals,
    stop_decision,
    structural_empty,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

STATUS_VERIFIED = "verified"
STATUS_STALE = "stale"

# RF2: a replay under this share of the previous verified set is suspect.
BASELINE_SHARE = 0.5

REPLAY_OK = "ok"
REPLAY_EMPTY = "empty"
REPLAY_ERROR = "error"


class ReplayClient(Protocol):
    """The three reads a replay needs; ``PoliteClient`` satisfies it."""

    def fetch(self, url: str) -> str: ...

    def fetch_rendered(self, url: str) -> str: ...

    def fetch_json(self, url: str) -> str: ...


@dataclass(frozen=True)
class ReplayOutcome:
    """What one production replay produced, and what the verifier said about it."""

    status: str
    postings: tuple[ScrapedJob, ...]
    verified_count: int
    statistics: ReplayStatistics | None
    signals: tuple[Signal, ...]
    gate: GateVerdict | None
    stopped: bool
    error: str | None
    rendered: bool
    agreement: DomAgreement | None
    page_url: str
    recipe_id: int | None = None
    # Why a paginated ``dom`` replay stopped fetching (``DOM_STOP_REASONS``).
    # ``single_page`` for a recipe that reads one document; None for an api
    # recipe. Evidence about the listing's depth, never a gate.
    page_stop: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == REPLAY_OK

    @property
    def gate_rejected(self) -> bool:
        """The posting-existence gate judged this replay's items not postings."""
        return self.ok and self.gate is not None and self.gate.is_none


def load_verified_recipe(
    session: Session, *, company_id: int | None = None, target_url: str | None = None
) -> CompanyRecipe | None:
    """The newest verified recipe for this owner, or None.

    Exactly one owner is required: a company careers page (``company_id``) or an
    M1 listing URL (``target_url``). A ``stale`` or ``candidate`` row is never
    replayed.
    """
    if (company_id is None) == (target_url is None):
        raise ValueError("exactly one of company_id or target_url is required")
    statement = select(CompanyRecipe).where(CompanyRecipe.status == STATUS_VERIFIED)
    if company_id is not None:
        statement = statement.where(CompanyRecipe.company_id == company_id)
    else:
        statement = statement.where(CompanyRecipe.target_url == target_url)
    return session.scalars(statement.order_by(CompanyRecipe.id.desc()).limit(1)).first()


def mark_stale(session: Session, recipe: CompanyRecipe) -> None:
    """Retire a recipe that no longer replays; the caller re-authors or falls back."""
    recipe.status = STATUS_STALE
    recipe.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()


def replay_stored_recipe(
    session: Session,
    *,
    run_source_id: int,
    recipe_row: CompanyRecipe,
    page_url: str,
    client: ReplayClient,
    llm: JsonCompleter | None = None,
    page_ceiling: int | None = None,
    drift_checks: bool = False,
) -> ReplayOutcome:
    """Replay one stored recipe and record its evidence on the run source.

    ``drift_checks`` adds two statistical signals that route a junk replay to the
    gate (RF2): a static ``dom`` replay below ``BASELINE_SHARE`` of the previous
    run's verified set is retried rendered first, and a rendered replay with
    zero DOM agreement fires. Thresholds are starting points, tuned on the
    25-company rerun.

    ``llm`` is the gate's completer. Without it the gate is skipped (the replay
    still records its verified set and statistics); the caller decides whether
    that is acceptable for its run type.

    ``page_ceiling`` is the run's runaway fuse (M1 ``name_collect``): it caps an
    ``api`` recipe's own ``pagination.max_pages`` for this replay and never
    raises it, so it cannot widen a recipe past its admitted body.
    """
    try:
        recipe = parse_body(recipe_row.body, recipe_row.kind)
    except ValueError as exc:
        return _error_outcome(page_url, f"stored recipe body rejected: {exc}")

    if page_ceiling is not None:
        # Both kinds carry ``pagination.max_pages``; the fuse can only lower it.
        recipe = recipe.model_copy(
            update={
                "pagination": recipe.pagination.model_copy(
                    update={"max_pages": min(recipe.pagination.max_pages, page_ceiling)}
                )
            }
        )

    html: str | None = None
    rendered = False
    agreement: DomAgreement | None = None
    page_stop: str | None = None
    below_baseline: tuple[int, int] | None = None
    try:
        if isinstance(recipe, ApiRecipe):
            report = run_api_recipe_report(recipe, client.fetch_json, page_url)
            postings = list(report.jobs)
            statistics = collect_statistics(
                postings,
                page_counts=report.page_counts,
                detail_attempted=report.detail_attempted,
                detail_succeeded=report.detail_succeeded,
            )
        else:
            html = client.fetch(page_url)
            try:
                dom = run_dom_recipe_report(recipe, client.fetch, page_url, first_html=html)
            except RecipeError:
                # One render retry when the static HTML has no names (name_collect
                # step 2). The rendered page is also the DOM-agreement arbiter, and
                # the retry pages through the same rendered tier: a site that needs
                # JS for page one needs it for every page.
                html = client.fetch_rendered(page_url)
                rendered = True
                dom = run_dom_recipe_report(recipe, client.fetch_rendered, page_url, first_html=html)
            if drift_checks:
                baseline = baseline_url_count(session, run_source_id)
                if baseline and not rendered and len(dom.jobs) < baseline * BASELINE_SHARE:
                    try:
                        rendered_html = client.fetch_rendered(page_url)
                        rendered_dom = run_dom_recipe_report(
                            recipe, client.fetch_rendered, page_url, first_html=rendered_html
                        )
                    except (RecipeError, RuntimeError, ValueError):
                        rendered_dom = None  # keep the static result; the signal still fires
                    if rendered_dom is not None and len(rendered_dom.jobs) > len(dom.jobs):
                        dom, html, rendered = rendered_dom, rendered_html, True
                if baseline and len(dom.jobs) < baseline * BASELINE_SHARE:
                    below_baseline = (len(dom.jobs), baseline)
            postings = list(dom.jobs)
            page_stop = dom.stop_reason
            statistics = collect_statistics(postings, page_counts=dom.page_counts)
            # Only the first document's postings can be on the rendered first page:
            # pages 2+ are by definition absent from it, so comparing all of them
            # would report every extra page as a replay-only miss.
            # A text-only recipe has no URLs to compare with the page's links.
            agreement = (
                dom_agreement(dom.first_page_jobs, html if rendered else None, page_url)
                if recipe.link is not None
                else None
            )
    except (RecipeError, RuntimeError, ValueError) as exc:
        return _error_outcome(page_url, str(exc), rendered=rendered)

    if not postings:
        # Both runners raise on an empty result; this is defensive. An observed
        # empty set is still evidence (a zero-yield replay), unlike a failure.
        verified_count = record_verified_urls(session, run_source_id, ())
        return ReplayOutcome(
            REPLAY_EMPTY, (), verified_count, statistics, (), None, False, None,
            rendered, agreement, page_url, recipe_row.id, page_stop,
        )

    verified_count = record_verified_urls(
        session, run_source_id, (job.url for job in postings)
    )
    fired = signals(recipe, statistics)
    if below_baseline is not None:
        fired += (
            Signal(
                "below_baseline",
                f"{below_baseline[0]}/{below_baseline[1]}",
                "replay yields under half of the previous run's postings, even rendered",
            ),
        )
    if drift_checks and agreement is not None and agreement.replay_count and agreement.matched == 0:
        fired += (
            Signal(
                "no_dom_agreement",
                f"0/{agreement.replay_count}",
                "no replayed URL is a link on the rendered page: selectors read something else",
            ),
        )
    gate, stopped = _run_gate(
        session, recipe_row, recipe, postings, page_url, llm, html, fired
    )
    return ReplayOutcome(
        REPLAY_OK, tuple(postings), verified_count, statistics, fired, gate,
        stopped, None, rendered, agreement, page_url, recipe_row.id, page_stop,
    )


def _run_gate(
    session: Session,
    recipe_row: CompanyRecipe,
    recipe: ApiRecipe | DomRecipe,
    postings: list[ScrapedJob],
    page_url: str,
    llm: JsonCompleter | None,
    html: str | None,
    fired: tuple[Signal, ...],
) -> tuple[GateVerdict | None, bool]:
    """Run the gate on its cadence and store its answer; return (verdict, stopped)."""
    if llm is None:
        return None, False
    digest = body_hash(recipe_row.body)
    # Read the prior answer before recording this one: a fresh "none" must not
    # count as its own independent agreement.
    prior_none = has_prior_none(session, digest)
    first_accepted = latest_flag(session, digest) is None
    if not should_run_gate(
        first_accepted=first_accepted, signal_fired=bool(fired), stop_cleared=False
    ):
        return None, False
    try:
        gate = run_posting_gate(
            recipe,
            postings,
            page_url,
            llm,
            dom_html=html if isinstance(recipe, DomRecipe) else None,
        )
    except GateError as exc:
        # R3: an unverifiable gate fails open — no flag, no stop.
        logger.warning("posting gate unusable for %s: %s", page_url, exc)
        return None, False
    stopped = stop_decision(
        verdict=gate.verdict,
        structural_empty=structural_empty(postings),
        prior_none=prior_none,
    )
    record_flag(
        session,
        body_sha256=digest,
        verdict=gate.verdict,
        analysis=gate.analysis(),
        stopped=stopped,
        model_id=gate.model_id,
        page_url=page_url,
        recipe_id=recipe_row.id,
    )
    return gate, stopped


def _error_outcome(page_url: str, error: str, *, rendered: bool = False) -> ReplayOutcome:
    return ReplayOutcome(
        REPLAY_ERROR, (), 0, None, (), None, False, error, rendered, None, page_url
    )


__all__ = [
    "REPLAY_EMPTY",
    "REPLAY_ERROR",
    "REPLAY_OK",
    "ReplayClient",
    "ReplayOutcome",
    "load_verified_recipe",
    "mark_stale",
    "replay_stored_recipe",
]
