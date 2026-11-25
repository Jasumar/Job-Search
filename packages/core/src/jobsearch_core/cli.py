from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click

from jobsearch_core.config import (
    Settings,
    load_defaults,
    load_filters_config,
    resolve_defaults,
)
from jobsearch_core.db.models import Application, CVRevision, Job
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.evidence_import import import_evidence_yaml
from jobsearch_core.filters import apply_filters, fit_flags_json
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger, setup_logging
from jobsearch_core.normalize import dedup_key, normalize
from jobsearch_core.profile_evidence import load_candidate_evidence, search_preferences
from jobsearch_core.report import generate_report
from jobsearch_core.run import match_jobs, match_one_job
from jobsearch_core.tailor import VALID_VARIANTS, tailor_cv

logger = get_logger(__name__)


@click.group()
@click.option("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
def main(log_level: str):
    setup_logging(log_level)


def _warn_deprecated(old_name: str, new_usage: str) -> None:
    click.echo(
        f"WARNING: '{old_name}' is deprecated (Phase 4.0a) and will be removed; "
        f"use '{new_usage}' instead.",
        err=True,
    )


@main.command()
@click.option("--db-url", envvar="DATABASE_URL", help="SQLite database URL")
def init(db_url: str | None):
    settings = Settings()
    url = db_url or settings.database_url
    db_path = Path(url.replace("sqlite:///", ""))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(url)
    click.echo(f"Database initialized at {db_path}")


@main.command()
@click.argument("url")
@click.option("--title", required=True, help="Raw job title")
@click.option("--company", required=True, help="Raw company name")
@click.option("--location", default="", help="Raw location string")
@click.option("--description", default="", help="Job description text")
@click.option("--authority-url", required=True, help="Verified employer careers or hiring URL")
@click.option("--posted-date", default=None, help="Posted date string (e.g. '2 days ago')")
def add(
    url: str,
    title: str,
    company: str,
    location: str,
    description: str,
    authority_url: str,
    posted_date: str | None,
):
    settings = Settings()
    config = load_filters_config()
    init_db(settings.database_url)

    norm_title, norm_company, norm_location, country = normalize(title, company, location)
    dkey = dedup_key(norm_title, norm_company, norm_location)

    session = get_session()

    existing = session.query(Job).filter(Job.dedup_hash == dkey).first()
    if existing:
        click.echo(f"Duplicate: job already exists as #{existing.id}")
        return

    result = apply_filters(
        title, company, location, description, config
    )

    job = Job(
        url=url,
        authority_url=authority_url,
        extraction_url=url,
        source_url=authority_url,
        raw_title=title,
        raw_company=company,
        raw_location=location,
        normalized_title=norm_title,
        normalized_company=norm_company,
        normalized_location=norm_location,
        country=country,
        description=description,
        posted_date=posted_date,
        dedup_hash=dkey,
        status="new",
        verification_status="verified",
        verified_at=datetime.now(UTC),
        fit_flags=fit_flags_json(result),
        included_families=(
            json.dumps(result.included_families) if result.included_families else None
        ),
        include_signals=(
            json.dumps(result.include_signals) if result.include_signals else None
        ),
    )
    session.add(job)
    session.commit()

    if not result.accepted:
        click.echo(f"Added job #{job.id}: {title} @ {company} (flagged: {result.reason})")
    else:
        click.echo(f"Added job #{job.id}: {title} @ {company}")


@main.command(name="score-job")
@click.argument("job_id", type=int)
def score_job(job_id: int):
    """Match a single job against the stored profile (Phase 4.1).

    Replaces the legacy five-dimension scoring command; the score is now a
    profile-vs-evidence match, and the recommendation group is reported
    instead of per-dimension values.
    """
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    try:
        evidence = load_candidate_evidence(session)
    finally:
        session.close()

    result = match_one_job(job_id=job_id, evidence=evidence, echo=click.echo)
    if result is None:
        click.echo(
            f"Job #{job_id} cannot be matched: unknown job, rejected, "
            "no description, or no structured job profile."
        )
        return
    click.echo(f"Job #{result.job_id} matched {result.score:.2%} — {result.recommendation}")
    for label, items in (
        ("Strengths", result.strengths),
        ("Gaps", result.gaps),
        ("Blockers", result.blockers),
    ):
        if items:
            click.echo(f"  {label}: {', '.join(items)}")


@main.command()
@click.option("--output", "-o", default="reports/report.md", help="Output file path")
def report(output: str):
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    jobs = session.query(Job).all()
    apps = session.query(Application).all()

    if not jobs:
        click.echo("No jobs in database. Run 'add' commands first.")
        return

    generate_report(jobs, output_path=output, applications=apps)
    click.echo(f"Report written to {output}")


def _run_match(
    output: str,
    limit: int | None,
    scope: str = "tech",
    max_liveness_checks: int | None = None,
    llm_notes: bool = False,
) -> None:
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    try:
        evidence = load_candidate_evidence(session)
    finally:
        session.close()

    match_jobs(
        settings=settings,
        evidence=evidence,
        output=output,
        limit=limit,
        scope=scope,
        max_liveness_checks=max_liveness_checks,
        llm_notes=llm_notes,
        echo=click.echo,
    )


@main.command()
@click.option("--output", "-o", default="reports/report.md", help="Output file path")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Maximum jobs to process")
@click.option(
    "--scope", default="tech", show_default=True,
    help="SearchScope name (config/scopes/<name>.yaml)",
)
@click.option(
    "--max-liveness-checks", type=click.IntRange(min=0), default=None,
    help="Maximum job-link liveness HEAD checks this run (default from config/defaults.yaml)",
)
@click.option(
    "--llm-notes", is_flag=True,
    help="Add bounded, cached LLM strengths/gaps/blockers for the top matches (default: deterministic only)",
)
def match(
    output: str,
    limit: int | None,
    scope: str,
    max_liveness_checks: int | None,
    llm_notes: bool,
):
    """Phase 4.1 - match: rank collected jobs against your profile.

    Deterministic and offline by default: every job with a structured profile
    is scored from that profile and your stored evidence, then grouped into
    worth applying / consider / skip. Every run scores every profiled job
    again; scoring is free.

    --llm-notes adds narrative explanations for the top matches only, capped by
    max_llm_notes in config/defaults.yaml and cached by prompt hash. Applying
    remains the separate `tailor` workflow behind its approval gate.
    """
    _run_match(
        output,
        limit,
        scope,
        max_liveness_checks,
        llm_notes,
    )


@main.command(hidden=True)
@click.option("--output", "-o", default="reports/report.md", help="Output file path")
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Maximum jobs to process")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Deprecated and ignored: Phase 4.1 matching is always deterministic and offline",
)
def pipeline(output: str, limit: int | None, dry_run: bool):
    """Deprecated alias for `jobsearch_core match`.

    The old ``--dry-run`` flag is still accepted so existing scripts keep
    working, but it does nothing: Phase 4.1 made the deterministic, offline
    match the *default* path, so there is no longer an LLM-backed run to skip.
    """
    _warn_deprecated("pipeline", "jobsearch_core match")
    _run_match(output, limit)

def _run_discovery_phase(
    *,
    dry_run: bool,
    country: str | None,
    role: str | None,
    search_backend: str | None,
    max_baseline_queries: int | None,
    max_companies: int | None,
    expand_queries: bool | None,
    max_variants: int | None,
    mine_listicles: bool | None,
    max_listicles: int | None,
    max_companies_per_listicle: int | None,
    scope: str,
) -> None:
    """Phase 1 - company identity discovery (Phase 4.0a).

    Resolves unset flags against ``config/defaults.yaml`` (explicit flag >
    defaults config > built-in default) and runs the discovery-and-confirm
    workflow, which stops at the review gate.
    """
    from jobsearch_core.run import print_run_summary, run_workflow

    defaults = load_defaults()
    resolved = resolve_defaults(
        defaults,
        search_backend=search_backend,
        max_baseline_queries=max_baseline_queries,
        max_companies=max_companies,
        expand_queries=expand_queries,
        max_variants=max_variants,
        mine_listicles=mine_listicles,
        max_listicles=max_listicles,
        max_companies_per_listicle=max_companies_per_listicle,
    )
    print_effective_settings(resolved)

    try:
        summary = run_workflow(
            dry_run=dry_run,
            country=country,
            role=role,
            search_backend=resolved["search_backend"],
            max_baseline_queries=resolved["max_baseline_queries"],
            max_companies=resolved["max_companies"],
            expand_queries=resolved["expand_queries"],
            max_variants_per_baseline=resolved["max_variants"],
            mine_listicles=resolved["mine_listicles"],
            max_listicles=resolved["max_listicles"],
            max_companies_per_listicle=resolved["max_companies_per_listicle"],
            scope=scope,
            echo=click.echo,
        )
    except RuntimeError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        raise SystemExit(1) from exc
    summary.effective_defaults = resolved
    print_run_summary(summary)


def print_effective_settings(effective: dict) -> None:
    """Echo the resolved per-run settings before a phase runs."""
    if not effective:
        return
    pairs = " ".join(f"{key}={value}" for key, value in sorted(effective.items()))
    click.echo(f"Effective settings: {pairs}")


_DISCOVERY_OPTIONS = [
    click.option("--dry-run", is_flag=True, help="Show what the run would do without writing to DB"),
    click.option("--country", default=None, type=click.Choice(["fi", "se", "ee", "dk", "de", "nl"]),
                 help="Country code to target for discovery"),
    click.option("--role", default=None, type=click.Choice(["backend", "data", "ai", "automation"]),
                 help="Role family to target for discovery"),
    click.option("--search-backend", default=None, type=click.Choice(["brave", "ddgs", "searxng"]),
                 help="Search backend to use (default from config/defaults.yaml)"),
    click.option("--max-baseline-queries", default=None, type=int,
                 help="Maximum baseline queries in identity-first discovery (default from config/defaults.yaml)"),
    click.option("--max-companies", default=None, type=int,
                 help="Maximum employer identities to follow up (default from config/defaults.yaml)"),
    click.option(
        "--expand-queries/--no-expand-queries", "expand_queries", default=None,
        help="Expand baseline queries with analysis-model variants "
        "(default from config/defaults.yaml; on by default since Phase 4.0a)",
    ),
    click.option(
        "--max-variants", default=None, type=int,
        help="Maximum generated query variants per baseline query (default from config/defaults.yaml)",
    ),
    click.option(
        "--mine-listicles/--no-mine-listicles", "mine_listicles", default=None,
        help="Mine multiple companies from hub/listicle pages (default from config/defaults.yaml)",
    ),
    click.option(
        "--max-listicles", default=None, type=int,
        help="Maximum listicle pages to fetch and mine per run (default from config/defaults.yaml)",
    ),
    click.option(
        "--max-companies-per-listicle", default=None, type=int,
        help="Maximum companies extracted from each mined listicle page (default from config/defaults.yaml)",
    ),
    click.option(
        "--scope", default="tech", show_default=True,
        help="SearchScope name (config/scopes/<name>.yaml) driving discovery prompts and templates",
    ),
]


def _discover_options(func):
    for option in reversed(_DISCOVERY_OPTIONS):
        func = option(func)
    return func


@main.group(invoke_without_command=True)
@_discover_options
@click.pass_context
def discover(ctx: click.Context, dry_run: bool, country: str | None, role: str | None,
             search_backend: str | None, max_baseline_queries: int | None,
             max_companies: int | None, expand_queries: bool | None, max_variants: int | None,
             mine_listicles: bool | None, max_listicles: int | None,
             max_companies_per_listicle: int | None, scope: str):
    """Phase 1 - discover companies via identity-first web search.

    Run without a subcommand to execute the full discovery pass: Stage A
    company-identity search, hub/listicle mining, Stage B hiring-source
    follow-ups, and persistence of new candidates to the review queue. The
    pass stops at the human review gate; verify candidates next with
    `jobsearch_core verify`.

    Subcommands (search, list, review, clear) remain available as lower-level
    utilities.
    """
    if ctx.invoked_subcommand is not None:
        return
    _run_discovery_phase(
        dry_run=dry_run,
        country=country,
        role=role,
        search_backend=search_backend,
        max_baseline_queries=max_baseline_queries,
        max_companies=max_companies,
        expand_queries=expand_queries,
        max_variants=max_variants,
        mine_listicles=mine_listicles,
        max_listicles=max_listicles,
        max_companies_per_listicle=max_companies_per_listicle,
        scope=scope,
    )


@main.command(hidden=True)
@_discover_options
def run(dry_run: bool, country: str | None, role: str | None,
        search_backend: str | None, max_baseline_queries: int | None,
        max_companies: int | None, expand_queries: bool | None, max_variants: int | None,
        mine_listicles: bool | None, max_listicles: int | None,
        max_companies_per_listicle: int | None, scope: str):
    """Deprecated alias for `jobsearch_core discover` (the discovery-and-confirm pass)."""
    _warn_deprecated("run", "jobsearch_core discover")
    _run_discovery_phase(
        dry_run=dry_run,
        country=country,
        role=role,
        search_backend=search_backend,
        max_baseline_queries=max_baseline_queries,
        max_companies=max_companies,
        expand_queries=expand_queries,
        max_variants=max_variants,
        mine_listicles=mine_listicles,
        max_listicles=max_listicles,
        max_companies_per_listicle=max_companies_per_listicle,
        scope=scope,
    )


def _collect_phase(
    *,
    output: str,
    dry_run: bool,
    max_sources: int | None,
    limit: int | None,
    no_details: bool,
    batch: int | None,
    refresh_known: bool,
    scope: str,
) -> None:
    """Phase 3 - scrape positions from confirmed sources (Phase 4.0a)."""
    from jobsearch_core.run import collect_workflow, print_run_summary

    summary = collect_workflow(
        dry_run=dry_run,
        max_sources=max_sources,
        limit=limit,
        no_details=no_details,
        output=output,
        batch=batch,
        refresh_known=refresh_known,
        scope=scope,
        echo=click.echo,
    )
    summary.effective_defaults = {
        "refresh_known": refresh_known,
        "batch": batch if batch is not None else "all accepted",
    }
    print_run_summary(summary)


@main.command()
@click.option("--output", "-o", default="reports/report.md", help="Output file path")
@click.option("--dry-run", is_flag=True, help="Show what the run would do without writing to DB")
@click.option("--max-sources", type=click.IntRange(min=1), default=None,
              help="Maximum accepted/verified sources to scrape this run")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Maximum eligible unscored jobs to score this run")
@click.option("--no-details", is_flag=True, help="Skip fetching detail pages (listings only)")
@click.option("--batch", type=click.IntRange(min=1), default=None,
              help="Maximum accepted companies to process this run (default: all accepted)")
@click.option("--refresh-known", is_flag=True,
              help="Also re-scrape all known companies to catch fresh postings")
@click.option(
    "--scope", default="tech", show_default=True,
    help="SearchScope name (config/scopes/<name>.yaml) driving dry-run scoring",
)
def collect(
    output: str,
    dry_run: bool,
    max_sources: int | None,
    limit: int | None,
    no_details: bool,
    batch: int | None,
    refresh_known: bool,
    scope: str,
):
    """Deprecated alias for `jobsearch_core scrape` (confirmed-source collection)."""
    _warn_deprecated("collect", "jobsearch_core scrape")
    _collect_phase(
        output=output,
        dry_run=dry_run,
        max_sources=max_sources,
        limit=limit,
        no_details=no_details,
        batch=batch,
        refresh_known=refresh_known,
        scope=scope,
    )


@main.command()
def verify():
    """Phase 2 - verify pending hiring-page candidates interactively.

    Opens each candidate's careers URL in the review companion and asks for
    accept / reject / skip / replace-URL. Only accepted candidates become
    scrapeable sources (Phase 3, `jobsearch_core scrape`).
    """
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import review_discoveries, review_discovery

    discoveries = review_discoveries("pending")
    if not discoveries:
        click.echo("No pending candidates to verify. Run `jobsearch_core discover` first.")
        return

    from jobsearch_core.review_companion import ReviewCompanion

    companion = ReviewCompanion(open_browser=True)
    companion_started = companion.start()
    if companion_started:
        click.echo(f"Review companion started at {companion.base_url}")
    else:
        click.echo("Review companion unavailable; proceeding headless.")

    accepted = 0
    rejected = 0
    skipped = 0
    try:
        for index, discovery in enumerate(discoveries):
            if companion_started:
                companion.set_current(
                    discovery.careers_url or discovery.source_url or "",
                    label=discovery.name,
                    index=index + 1,
                    total=len(discoveries),
                )
            click.echo(f"\n#{discovery.id} {discovery.name}")
            click.echo(f"Candidate: {discovery.careers_url or '-'}")
            if discovery.source_url and discovery.source_url != discovery.careers_url:
                click.echo(f"Source: {discovery.source_url}")
            click.echo(f"Resolution: {discovery.resolution_status}/{discovery.resolution_page_type}")
            action = click.prompt(
                "Action [a]ccept, [r]eject, [s]kip, [u]pdate URL",
                type=click.Choice(["a", "r", "s", "u"], case_sensitive=False),
            ).lower()
            if action == "a":
                careers_url = discovery.careers_url
            elif action == "r":
                review_discovery(discovery.id, "rejected")
                rejected += 1
                click.echo("  rejected")
                continue
            elif action == "u":
                careers_url = click.prompt("Verified careers URL")
            else:
                skipped += 1
                click.echo("  skipped")
                continue

            if careers_url:
                review_discovery(discovery.id, "accepted", careers_url=careers_url)
                accepted += 1
                click.echo("  accepted (stored in the companies table)")
    finally:
        if companion_started:
            companion.mark_complete()
            companion.close()

    click.echo(
        f"\nVerification complete: {accepted} accepted, {rejected} rejected, {skipped} skipped."
    )
    if accepted:
        click.echo("Next: run `jobsearch_core scrape` to collect positions from confirmed sources.")


@main.command()
@click.option("--output", "-o", default="reports/report.md", help="Output file path")
@click.option("--dry-run", is_flag=True, help="Show what the run would do without writing to DB")
@click.option("--max-sources", type=click.IntRange(min=1), default=None,
              help="Maximum accepted/verified sources to scrape this run")
@click.option("--limit", type=click.IntRange(min=1), default=None,
              help="Maximum eligible unscored jobs to score this run")
@click.option("--no-details", is_flag=True, help="Skip fetching detail pages (listings only)")
@click.option("--batch", type=click.IntRange(min=1), default=None,
              help="Maximum accepted companies to process this run (default: all accepted)")
@click.option("--refresh-known", is_flag=True,
              help="Also re-scrape all known companies to catch fresh postings")
@click.option(
    "--scope", default="tech", show_default=True,
    help="SearchScope name (config/scopes/<name>.yaml) driving dry-run scoring",
)
@click.argument("url", required=False)
@click.option("--company", "company_name", default=None, help="Company name (required when scraping a raw URL)")
@click.option("--all-companies", "all_companies", is_flag=True,
              help="Scrape all known + accepted companies from the companies table")
@click.option("--discovery", "discovery", is_flag=True,
              help="Scrape all accepted companies from the companies table")
def scrape(
    output: str,
    dry_run: bool,
    max_sources: int | None,
    limit: int | None,
    no_details: bool,
    batch: int | None,
    refresh_known: bool,
    scope: str,
    url: str | None,
    company_name: str | None,
    all_companies: bool,
    discovery: bool,
):
    """Phase 3 - scrape positions from verified hiring pages.

    Run without arguments to collect from all confirmed sources (accepted
    companies, plus known companies with --refresh-known), score eligible
    jobs, and write the report. Pass a raw URL (with --company) or use
    --all-companies / --discovery for the lower-level utility paths.
    """
    from jobsearch_core.ingest.scrapers.pipeline import (
        ingest_scraped,
        scrape_company,
        scrape_company_entries,
    )

    if url or all_companies or discovery:
        _scrape_utility(
            url=url,
            company_name=company_name,
            all_companies=all_companies,
            discovery=discovery,
            dry_run=dry_run,
            no_details=no_details,
            scrape_company=scrape_company,
            ingest_scraped=ingest_scraped,
            scrape_company_entries=scrape_company_entries,
        )
        return

    _collect_phase(
        output=output,
        dry_run=dry_run,
        max_sources=max_sources,
        limit=limit,
        no_details=no_details,
        batch=batch,
        refresh_known=refresh_known,
        scope=scope,
    )


def _scrape_utility(
    *,
    url: str | None,
    company_name: str | None,
    all_companies: bool,
    discovery: bool,
    dry_run: bool,
    no_details: bool,
    scrape_company,
    ingest_scraped,
    scrape_company_entries,
) -> None:
    """The pre-existing scrape utility paths (raw URL, --all-companies, --discovery)."""
    settings = Settings()
    init_db(settings.database_url)

    if all_companies:
        click.echo("Scraping all known + accepted companies...")
        stats = scrape_company_entries(
            dry_run=dry_run,
            fetch_descriptions=not no_details,
            refresh_known=True,
        )
        click.echo(
            f"\nDone. {stats['companies']} company(ies) scraped: "
            f"{stats['total_added']} added ({stats['total_flagged']} flagged), "
            f"{stats['total_dup']} duplicates, {stats['total_no_desc']} no-description, "
            f"{stats['promoted']} promoted to known."
        )
        return

    if discovery:
        click.echo("Scraping accepted companies...")
        stats = scrape_company_entries(
            dry_run=dry_run,
            fetch_descriptions=not no_details,
        )
        click.echo(
            f"\nDone. {stats['companies']} company(ies) scraped: "
            f"{stats['total_added']} added ({stats['total_flagged']} flagged), "
            f"{stats['total_dup']} duplicates, {stats['total_no_desc']} no-description, "
            f"{stats['promoted']} promoted to known."
        )
        return

    if not url:
        click.echo("Provide a URL, or use --all-companies / --discovery.")
        return

    # Phase 4.1e: a manually passed URL is a human-verified employer origin.
    # A board URL is still refused — boards are reachable only through a link
    # on the employer's own careers page, never as the source.
    from jobsearch_core.ingest.scrapers.pipeline import _classify_ats_url

    board = _classify_ats_url(url)
    if board:
        click.echo(
            f"Refusing to scrape {url} — it is an ATS board URL ({board}). "
            "Scrape the employer's own careers page instead; a delegated board "
            "is reached through links on that page."
        )
        raise SystemExit(1)

    company = company_name
    if not company:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.replace("www.", "").split(".")[0]
        company = host.title()

    click.echo(f"Scraping {company} @ {url}...")
    jobs = scrape_company(company, url, fetch_descriptions=not no_details, verified=True)

    if not jobs:
        click.echo("No job listings found.")
        return

    click.echo(f"Found {len(jobs)} listing(s).")
    stats = ingest_scraped(jobs, source_url=url, dry_run=dry_run)
    click.echo(
        f"Result: {stats['added']} added ({stats['flagged']} flagged), "
        f"{stats['skipped_dup']} duplicates, {stats['no_description']} no-description, "
        f"{stats['unverified']} unverified."
    )


@main.command()
@click.argument("job_id", type=int)
@click.option(
    "--variant",
    default="backend",
    type=click.Choice(sorted(VALID_VARIANTS)),
    help="Base CV variant to tailor",
)
@click.option("--yes", "-y", is_flag=True, help="Skip approval prompt and approve immediately")
def tailor(job_id: int, variant: str, yes: bool):
    """Tailor a CV for a scored job and request human approval."""
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    evidence = load_candidate_evidence(session)
    job = session.query(Job).filter(Job.id == job_id).first()
    if not job:
        click.echo(f"Job #{job_id} not found")
        return
    if not job.description:
        click.echo(f"Job #{job_id} has no description; cannot tailor")
        return

    existing = session.query(Application).filter(Application.job_id == job_id).first()
    if existing:
        click.echo(f"Job #{job_id} already has an application (status: {existing.status})")
        return

    client = LLMClient(api_key=settings.openrouter_api_key, model=settings.application_model)
    result = tailor_cv(evidence, job.description, variant, client, settings.llm_cache_dir)

    if result.cache_hit:
        click.echo("(cached result)")
    click.echo("--- unified diff ---")
    click.echo(result.diff)
    click.echo("--- end diff ---")

    if not yes:
        click.echo("")
        approved = click.confirm("Approve this tailored CV?", default=False)
    else:
        approved = True

    if not approved:
        click.echo("Tailoring discarded.")
        return

    apps_dir = Path("data/applications")
    apps_dir.mkdir(parents=True, exist_ok=True)
    app_dir = apps_dir / f"job_{job_id}"
    app_dir.mkdir(parents=True, exist_ok=True)
    typst_path = app_dir / "cv.typst"
    typst_path.write_text(result.tailored, encoding="utf-8")

    application = Application(
        job_id=job.id,
        base_variant=variant,
        status="approved",
        tailored_typst_path=str(typst_path),
    )
    session.add(application)
    session.flush()

    revision = CVRevision(
        application_id=application.id,
        diff_text=result.diff,
    )
    session.add(revision)
    session.commit()

    click.echo(f"Application #{application.id} created. Tailored CV at {typst_path}")

    from jobsearch_core.cv.compile import compile_typst, is_typst_available
    if is_typst_available():
        click.echo("Compiling CV...")
        try:
            pdf_path = compile_typst(typst_path)
            click.echo(f"CV compiled to {pdf_path}")
        except RuntimeError as exc:
            click.echo(f"CV compilation failed: {exc}")
            session.delete(application)
            session.commit()
            typst_path.unlink(missing_ok=True)
            click.echo("Application discarded due to overflow.")
            return
    else:
        click.echo("Typst CLI not found; skipping compilation. Install with: winget install typst")


@main.command()
@click.option("--status", default=None, help="Filter by status")
def applications(status: str | None):
    """List all applications."""
    settings = Settings()
    init_db(settings.database_url)

    session = get_session()
    query = session.query(Application)
    if status:
        query = query.filter(Application.status == status)
    apps = query.all()

    if not apps:
        click.echo("No applications found.")
        return

    for app in apps:
        job = app.job
        click.echo(
            f"#{app.id} | Job #{job.id}: {job.raw_title} @ {job.raw_company} "
            f"| variant={app.base_variant} | status={app.status} "
            f"| created={app.created_at.strftime('%Y-%m-%d')}"
        )


@main.group()
def jobs():
    """Query jobs stored in the database."""


@jobs.command(name="list")
@click.option("--status", default=None, help="Filter by job status (e.g. new, rejected)")
@click.option("--country", default=None, help="Filter by country code (e.g. fi, se, de)")
@click.option(
    "--min-score",
    type=click.FloatRange(0.0, 1.0),
    default=None,
    help="Only jobs with total_score >= this value (0.0 to 1.0)",
)
@click.option("--scored/--unscored", "scored", default=None, help="Filter to scored or unscored jobs")
@click.option(
    "--tailored/--not-tailored",
    "tailored",
    default=None,
    help="Filter by whether a tailored application exists",
)
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Maximum number of jobs to show")
def list_jobs(
    status: str | None,
    country: str | None,
    min_score: float | None,
    scored: bool | None,
    tailored: bool | None,
    limit: int | None,
):
    """List jobs to pick job_ids for scoring and tailoring."""
    from jobsearch_core.run import list_jobs

    jobs = list_jobs(
        status=status,
        country=country,
        min_score=min_score,
        scored=scored,
        tailored=tailored,
        limit=limit,
    )

    if not jobs:
        click.echo("No jobs match the given filters.")
        return

    click.echo(
        f"{'ID':<5}{'STATUS':<10}{'SCORE':<8}{'CN':<4}{'TAIL':<6}{'POSTED':<12}TITLE @ COMPANY"
    )
    click.echo("-" * 90)
    for job in jobs:
        score = f"{job.total_score:.0%}" if job.total_score is not None else "-"
        tailored_flag = "yes" if job.applications else ""
        posted = (job.posted_date or "").strip()[:11]
        click.echo(
            f"{job.id:<5}{job.status:<10}{score:<8}{(job.country or ''):<4}"
            f"{tailored_flag:<6}{posted:<12}{job.raw_title} @ {job.raw_company}"
        )


@main.command(name="import-evidence")
@click.option(
    "--path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default="data/evidence.yaml",
    help="Legacy evidence.yaml to import.",
)
def import_evidence(path: Path):
    """One-time migration of ``data/evidence.yaml`` into the database (Phase 4.1a).

    Idempotent: existing rows are replaced, so re-running yields the same
    state. After this succeeds the YAML file can be deleted — nothing reads it.
    """
    settings = Settings()
    source = Path(path)
    if not source.exists():
        raise click.ClickException(f"No evidence.yaml at {source}")
    init_db(settings.database_url)
    session = get_session()
    try:
        report = import_evidence_yaml(session, source)
        session.commit()
    finally:
        session.close()
    click.echo(f"Imported {source}")
    for key, value in report.items():
        click.echo(f"  {key}: {value}")


@main.group()
def company():
    """Query the companies table (the single source of truth for hiring sources)."""


@company.command(name="list")
@click.option("--status", default=None, help="Filter by status (pending, accepted, known, rejected)")
@click.option("--country", default=None, help="Filter by country code (e.g. FI)")
@click.option("--limit", default=None, type=int, help="Maximum rows to show")
def company_list(status: str | None, country: str | None, limit: int | None):
    """List companies in the companies table."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import list_companies

    rows = list_companies(status=status, country=country, limit=limit)
    if not rows:
        click.echo("No companies match the given filters.")
        return
    click.echo(f"{'ID':<5}{'STATUS':<10}{'CN':<4}NAME | CAREERS URL")
    click.echo("-" * 90)
    for c in rows:
        click.echo(
            f"{c.id:<5}{c.status:<10}{(c.country or ''):<4}{c.name} | {c.careers_url or '-'}"
        )


@company.command()
def count():
    """Show company counts per status and per country."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import count_companies

    counts = count_companies()
    click.echo("By status:")
    for status, n in sorted(counts["by_status"].items()):
        click.echo(f"  {status:<10} {n}")
    click.echo("By country:")
    for country, n in sorted(counts["by_country"].items()):
        click.echo(f"  {(country or '-'):<10} {n}")


@company.command(name="show")
@click.argument("company_id", type=int)
def company_show(company_id: int):
    """Show the full record for one company."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import get_company

    company = get_company(company_id)
    if not company:
        click.echo(f"Company #{company_id} not found.")
        return
    fields = {
        "id": company.id,
        "name": company.name,
        "status": company.status,
        "country": company.country,
        "careers_url": company.careers_url,
        # Phase 4.0d: the resolved listing page and the hop that found it.
        "open_positions_url": company.open_positions_url,
        "open_positions_landing_url": company.open_positions_landing_url,
        "open_positions_link_text": company.open_positions_link_text,
        "source_url": company.source_url,
        "ats_platform": company.ats_platform,
        "verification_status": company.verification_status,
        "verified_at": company.verified_at,
        "notes": company.notes,
        "employer_domain": company.employer_domain,
        "identity_name": company.identity_name,
        "followup_query": company.followup_query,
        "resolution_status": company.resolution_status,
        "discovered_at": company.discovered_at,
    }
    for key, value in fields.items():
        click.echo(f"{key}: {value}")


@company.command(name="add")
@click.option("--name", required=True, help="Company name")
@click.option("--careers-url", "careers_url", required=True, help="Canonical careers URL")
@click.option("--notes", default="", help="Operational notes")
@click.option("--country", default=None, help="Country code (e.g. FI)")
def company_add(name: str, careers_url: str, notes: str, country: str | None):
    """Manually add a curated company as a durable known source."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import add_company

    company = add_company(
        name=name,
        careers_url=careers_url,
        notes=notes,
        country=country,
    )
    click.echo(f"Added company #{company.id} '{company.name}' as known.")


@discover.command()
@click.option("--country", default=None, type=click.Choice(["fi", "se", "ee", "dk", "de", "nl"]),
              help="Country code to target")
@click.option("--role", default=None, type=click.Choice(["backend", "data", "ai", "automation"]),
              help="Role family to target")
@click.option("--dry-run", is_flag=True, help="Print candidates without saving to DB")
@click.option("--search-backend", default=None, type=click.Choice(["brave", "ddgs", "searxng"]),
              help="Search backend to use (default from config/defaults.yaml)")
@click.option("--num-results", default=10, type=int, help="Results per query")
@click.option("--resolve-links", is_flag=True, help="Probe job-page parent URLs and resolve likely careers pages")
@click.option("--triage", is_flag=True, help="Use the analysis model to triage filtered search results in batches")
@click.option(
    "--identity-first/--no-identity-first", default=True, show_default=True,
    help="Treat results as company-identity signals and follow up per employer "
    "(the default discovery mode; classic single-pass mode via --no-identity-first)",
)
@click.option(
    "--max-baseline-queries", default=8, type=int, show_default=True,
    help="Maximum baseline queries in identity-first mode",
)
@click.option(
    "--max-companies", default=10, type=int, show_default=True,
    help="Maximum employer identities to follow up",
)
@click.option(
    "--expand-queries/--no-expand-queries", "expand_queries", default=True, show_default=True,
    help="Use the analysis model to expand baseline queries with country tech-hub variants "
    "(default from config/defaults.yaml; on by default since Phase 4.0a)",
)
@click.option(
    "--max-variants", default=2, type=int, show_default=True,
    help="Maximum generated query variants per baseline query",
)
@click.option(
    "--mine-listicles/--no-mine-listicles", default=True, show_default=True,
    help="Mine multiple companies from hub/listicle pages (identity-first only; "
    "ignored in classic mode)",
)
@click.option(
    "--max-listicles", default=3, type=int, show_default=True,
    help="Maximum listicle pages to fetch and mine per run",
)
@click.option(
    "--max-companies-per-listicle", default=15, type=int, show_default=True,
    help="Maximum companies extracted from each mined listicle page",
)
@click.option(
    "--scope", default="tech", show_default=True,
    help="SearchScope name (config/scopes/<name>.yaml) driving discovery prompts and templates",
)
def search(
    country: str | None,
    role: str | None,
    dry_run: bool,
    search_backend: str | None,
    num_results: int,
    resolve_links: bool,
    triage: bool,
    identity_first: bool,
    max_baseline_queries: int,
    max_companies: int,
    expand_queries: bool,
    max_variants: int,
    mine_listicles: bool,
    max_listicles: int,
    max_companies_per_listicle: int,
    scope: str,
):
    """Run web search to discover new companies.

    Identity-first is the default mode: results are treated as company-identity
    signals, each surviving signal is routed as hub/single/noise, hub pages are
    mined for many companies, and each new employer identity is followed up with
    bounded hiring-source searches. Pass --no-identity-first for the classic
    single-pass mode (mining is ignored there).
    """
    from jobsearch_core.config import load_scope
    from jobsearch_core.search.discover import discover_with_summary as run_discover
    from jobsearch_core.search.templates import build_company_segment_queries, build_queries

    resolved_scope = load_scope(scope)
    if identity_first:
        query_count = len(build_company_segment_queries(country, resolved_scope))
        mode_note = " (company-segment Stage A baselines)"
    else:
        query_count = len(build_queries(country, role, resolved_scope))
        mode_note = ""
    if not country and not role:
        click.echo(f"WARNING: no --country or --role specified — running {query_count} queries (full sweep). "
                    "Pass --country and/or --role to narrow the search.")
    else:
        click.echo(f"Running {query_count} queries{mode_note}...")

    settings = Settings()
    # The subcommand's flag wins; otherwise the approved default (config/defaults.yaml).
    search_backend = search_backend or load_defaults().search_backend
    init_db(settings.database_url)

    if search_backend == "searxng":
        from jobsearch_core.search.client import SearXNGSearchClient, _ensure_searxng

        client = SearXNGSearchClient()
        try:
            _ensure_searxng(client)
        except RuntimeError as exc:
            click.echo(f"ERROR: {exc}", err=True)
            raise SystemExit(1) from exc
    else:
        from jobsearch_core.search.client import build_search_client

        try:
            client = build_search_client(search_backend, api_key=settings.brave_search_key)
        except (RuntimeError, ValueError) as exc:
            click.echo(f"ERROR: {exc}", err=True)
            raise SystemExit(1) from exc

    from jobsearch_core.search.careers_search import AdaptivePacer, PacedSearchClient

    # Space the queries: a free engine gets human-scale delays, the paid one one
    # request per second. Same rule as run_workflow and careers_search.
    client = PacedSearchClient(client, AdaptivePacer.for_backend(search_backend))

    http_client = None
    llm_client = None
    if resolve_links or triage or identity_first or expand_queries:
        from jobsearch_core.ingest.http import PoliteClient

        http_client = PoliteClient()
        llm_client = LLMClient(
            api_key=settings.openrouter_api_key,
            model=settings.analysis_model,
            base_url=settings.llm_base_url,
            min_interval_seconds=settings.llm_min_interval_seconds,
            max_retries=settings.llm_max_retries,
        )

    preferences = None
    if expand_queries:
        session = get_session()
        try:
            preferences = search_preferences(
                load_candidate_evidence(session), source="db"
            )
        finally:
            session.close()

    try:
        candidates, summary = run_discover(
            client,
            country,
            role,
            dry_run=dry_run,
            num_results=num_results,
            resolve_links=resolve_links,
            http_client=http_client,
            llm_client=llm_client,
            cache_dir=settings.llm_cache_dir,
            triage=triage,
            identity_first=identity_first,
            max_baseline_queries=max_baseline_queries,
            max_companies=max_companies,
            expand_queries=expand_queries,
            max_variants_per_baseline=max_variants,
            preferences=preferences,
            mine_listicles=mine_listicles,
            max_listicles=max_listicles,
            max_companies_per_listicle=max_companies_per_listicle,
            scope=resolved_scope,
        )
    finally:
        if http_client:
            http_client.close()
        if hasattr(client, "close"):
            client.close()

    if not candidates:
        click.echo("No candidates found.")
        return

    click.echo(f"\nFound {len(candidates)} candidate{'s' if len(candidates) != 1 else ''}:")
    for i, c in enumerate(candidates, 1):
        ats_tag = f" [{c['ats_platform']}]" if c["ats_platform"] else ""
        resolution_tag = f" ({c['resolution_status']})" if resolve_links else ""
        page_type = f" [{c['resolution_page_type']}]" if resolve_links else ""
        click.echo(f"  {i}. {c['name']}{ats_tag}{resolution_tag}{page_type} — {c['careers_url']}")
        if resolve_links and c["source_url"] != c["careers_url"]:
            click.echo(f"     source: {c['source_url']}")
        if identity_first:
            domain_tag = f" employer_domain={c['employer_domain']}" if c.get("employer_domain") else ""
            click.echo(f"     identity: {c['identity_name']} ({c['identity_confidence']:.2f}){domain_tag}")
            click.echo(f"     signal: {c['source_url']}")
            click.echo(f"     follow-up: {c['followup_query']}")
    if identity_first:
        from jobsearch_core.search.discover import DiscoveryBudget

        budget = DiscoveryBudget(
            max_baseline_queries=max_baseline_queries,
            max_companies=max_companies,
            max_variants_per_baseline=max_variants if expand_queries else 0,
        )
        click.echo(
            f"\nIdentity-first request budget: up to {budget.total_search_requests} search requests "
            f"({budget.max_baseline_queries} baseline queries"
            f"{' x ' + str(budget.max_variants_per_baseline) + ' variants' if budget.max_variants_per_baseline else ''}"
            f" + {budget.max_companies} companies x "
            f"{budget.max_followup_queries_per_company} follow-up)."
        )
        click.echo(
            f"Run summary: {summary.search_requests_used} requests used, "
            f"{summary.known_companies} known companies suppressed, "
            f"{summary.known_alternate_urls} alternate hiring URLs for known companies, "
            f"{summary.new_companies} new company candidates."
        )
        if summary.identity_signals:
            click.echo(
                f"Signals: {summary.identity_signals} identity signals, "
                f"{summary.sources_selected} hiring sources selected."
            )
        if summary.listicle_fetches:
            click.echo(
                f"Listicles: {summary.listicle_fetches} page(s) mined, "
                f"{summary.listicle_companies} company(ies) mined from them."
            )
        if summary.listicle_renders:
            click.echo(
                f"Client-rendered listicles: {summary.listicle_renders} page(s) re-fetched "
                f"through a browser."
            )
        if summary.hub_routed or summary.noise_dropped:
            click.echo(
                f"Routing: {summary.hub_routed} hub signal(s) routed to mining, "
                f"{summary.noise_dropped} noise signal(s) dropped."
            )
        if summary.candidates_persisted:
            click.echo(f"{summary.candidates_persisted} candidate(s) persisted to the review queue.")
        if summary.identity_signals and not summary.candidates_persisted:
            click.echo("Remaining manual action: review new candidates with `jobsearch_core discover review`.")
    if dry_run:
        click.echo("\n(Dry-run: nothing saved to DB)")


@discover.command(name="list")
@click.option("--status", default="pending", help="Filter by status (pending, reviewed, accepted, rejected)")
def list_discoveries(status: str):
    """List discovered companies in the review queue."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import review_discoveries
    results = review_discoveries(status_filter=status)

    if not results:
        click.echo(f"No discoveries with status '{status}'.")
        return

    click.echo(f"\nDiscovered companies (status: {status}):")
    for dc in results:
        ats_tag = f" [{dc.ats_platform}]" if dc.ats_platform else ""
        click.echo(
            f"  #{dc.id} | {dc.name}{ats_tag} | {dc.careers_url or '-'} | "
            f"{dc.resolution_status}/{dc.resolution_page_type} | {dc.country or '-'}"
        )


@discover.command()
@click.argument("ids", nargs=-1, type=int, required=False)
@click.option("--status", default="accepted", type=click.Choice(["accepted", "rejected"]), help="New status")
@click.option("--interactive", is_flag=True, help="Review pending discoveries one at a time")
def review(ids: tuple[int, ...], status: str, interactive: bool):
    """Accept or reject discovered companies by ID."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import review_discoveries, review_discovery, update_discovery_status

    if interactive:
        discoveries = review_discoveries("pending") if not ids else [
            dc for dc in review_discoveries("") if dc.id in ids
        ]

        from jobsearch_core.review_companion import ReviewCompanion

        companion = ReviewCompanion(open_browser=True)
        companion_started = companion.start()
        if companion_started:
            click.echo(f"Review companion started at {companion.base_url}")
        else:
            click.echo("Review companion unavailable; proceeding headless.")

        try:
            for index, discovery in enumerate(discoveries):
                if companion_started:
                    companion.set_current(
                        discovery.careers_url or discovery.source_url or "",
                        label=discovery.name,
                        index=index + 1,
                        total=len(discoveries),
                    )
                click.echo(f"\n#{discovery.id} {discovery.name}")
                click.echo(f"Candidate: {discovery.careers_url or '-'}")
                if discovery.source_url and discovery.source_url != discovery.careers_url:
                    click.echo(f"Source: {discovery.source_url}")
                click.echo(f"Resolution: {discovery.resolution_status}/{discovery.resolution_page_type}")
                action = click.prompt(
                    "Action [a]ccept, [r]eject, [s]kip, [u]pdate URL",
                    type=click.Choice(["a", "r", "s", "u"], case_sensitive=False),
                ).lower()
                result_label = "skipped"
                if action == "a":
                    careers_url = discovery.careers_url
                elif action == "r":
                    review_discovery(discovery.id, "rejected")
                elif action == "u":
                    careers_url = click.prompt("Verified careers URL")
                else:
                    careers_url = None

                if action in {"a", "u"} and careers_url:
                    review_discovery(discovery.id, "accepted", careers_url=careers_url)
                    click.echo("  accepted (stored in the companies table)")
                    result_label = "accepted"
                elif action == "r":
                    result_label = "rejected"
                action_labels = {
                    "a": "accepted",
                    "r": "rejected",
                    "s": "skipped",
                    "u": "reviewed with updated URL",
                }
                if action in {"a", "u"} and result_label == "skipped":
                    click.echo("  skipped")
                else:
                    click.echo(f"  {result_label or action_labels[action]}")
        finally:
            if companion_started:
                companion.mark_complete()
                companion.close()
        return

    if not ids:
        raise click.UsageError("Provide discovery IDs or use --interactive")
    count = update_discovery_status(list(ids), status)
    click.echo(f"Updated {count} discovery record(s) to '{status}'.")


@discover.command()
@click.option(
    "--status",
    default="pending",
    type=click.Choice(["pending", "accepted", "rejected", "all"]),
    help="Which discovery records to clear (default: pending). companies.yaml is never touched.",
)
def clear(status: str):
    """Delete discovered candidates from the review queue (never touches companies.yaml)."""
    settings = Settings()
    init_db(settings.database_url)

    from jobsearch_core.search.discover import clear_discovery_queue

    count = clear_discovery_queue(status)
    scope = "all" if status == "all" else f"'{status}'"
    click.echo(f"Removed {count} company record(s) with status {scope}.")
    click.echo("known companies are protected and were not removed.")


@main.command()
@click.option("--limit", default=10, type=int, help="Max models to show")
@click.option("--sort", default="most-popular", help="Sort order")
@click.option("--filter-free", is_flag=True, help="Show only free models")
def models(limit: int, sort: str, filter_free: bool):
    """List available models from OpenRouter."""
    settings = Settings()
    if not settings.openrouter_api_key or settings.openrouter_api_key.startswith("sk-or-v1-..."):
        click.echo("OPENROUTER_API_KEY not configured. Set it in .env or environment.")
        return

    client = LLMClient(api_key=settings.openrouter_api_key)
    result = client._client.models.list(limit=limit, sort=sort)
    model_list = result.result.data if result else []

    if not model_list:
        click.echo("No models returned.")
        return

    click.echo(f"\n{'ID':<40} {'Name':<40} {'Context':<10} {'Price (in/out)':<20}")
    click.echo("-" * 110)
    count = 0
    for m in model_list:
        if filter_free and (m.pricing.prompt != 0 or m.pricing.completion != 0):
            continue
        context = f"{m.context_length // 1000}k" if m.context_length else "?"
        price = f"${m.pricing.prompt:.6f}/${m.pricing.completion:.6f}"
        click.echo(f"{m.id:<40} {m.name[:38]:<40} {context:<10} {price:<20}")
        count += 1

    click.echo(f"\n{count} model(s) shown.")


@main.command()
def ping():
    """Verify OpenRouter API connectivity with a lightweight request."""
    settings = Settings()
    if not settings.openrouter_api_key or settings.openrouter_api_key.startswith("sk-or-v1-..."):
        click.echo("OPENROUTER_API_KEY not configured. Set it in .env or environment.")
        return

    client = LLMClient(api_key=settings.openrouter_api_key)
    try:
        result = client._client.models.list(limit=1)
        if result and result.result.data:
            click.echo(f"OK — API reachable. First model: {result.result.data[0].id}")
        else:
            click.echo("OK — API reachable but no models returned.")
    except Exception as exc:
        click.echo(f"FAIL — {exc}")


if __name__ == "__main__":
    main()
