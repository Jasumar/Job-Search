from __future__ import annotations

import json
from datetime import UTC, datetime

from jobsearch_core.config import load_defaults
from jobsearch_core.db.models import Application, Job
from jobsearch_core.scoring.match import (
    CONSIDER,
    SKIP,
    WORTH,
    load_job_profile,
    match_config_from_defaults,
)


def generate_report(
    jobs: list[Job],
    output_path: str | None = None,
    applications: list[Application] | None = None,
    recommendations: dict[str, list[dict]] | None = None,
) -> str:
    """Render the job-search report.

    Phase 4.1: the ranking section is generated from persisted match data and is
    grouped into worth applying / consider / skip. When the caller passes
    ``recommendations`` (the match run's freshly computed groups) those are
    rendered as-is; otherwise the groups are rebuilt from the persisted columns,
    so a report can be regenerated at any time without re-running the match and
    without any LLM call.

    The legacy per-dimension score table is no longer rendered: Phase 4.1
    retired the five-dimension writer. Existing ``scores`` rows stay in the DB
    until the end-of-roadmap repo cleanup drops that table.
    """
    config = match_config_from_defaults(load_defaults())

    if recommendations is None:
        recommendations, unranked = _recommendations_from_jobs(jobs, config)
    else:
        unranked = []

    apps = applications or []
    pending = [j for j in jobs if j.status == "new" and j.total_score is None]

    lines: list[str] = []
    lines.append("# Job Search Report")
    lines.append(f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    if apps:
        lines.append("## Applications\n")
        for app in apps:
            job = app.job
            lines.append(
                f"- **{job.raw_title} @ {job.raw_company}** — "
                f"variant: `{app.base_variant}`, status: {app.status}"
            )
            if app.tailored_typst_path:
                lines.append(f"  CV: {app.tailored_typst_path}")
            if app.revisions:
                rev = app.revisions[-1]
                if rev.approved_at:
                    lines.append(f"  Approved: {rev.approved_at.strftime('%Y-%m-%d %H:%M')}")
            lines.append("")

    _append_group(lines, "Worth Applying", recommendations.get(WORTH, []))
    _append_group(lines, "Consider", recommendations.get(CONSIDER, []))
    _append_group(lines, "Skip", recommendations.get(SKIP, []))

    if unranked:
        lines.append("## Pending (no structured job profile — needs a match run)\n")
        for job in unranked:
            lines.append(f"- {job.raw_title} @ {job.raw_company} ({job.url})")
            lines.append("")

    if pending:
        lines.append("## Pending (not yet matched)\n")
        for job in pending:
            lines.append(f"- {job.raw_title} @ {job.raw_company} ({job.url})")
            lines.append("")

    report = "\n".join(lines)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)

    return report


def _append_group(lines: list[str], heading: str, entries: list[dict]) -> None:
    if not entries:
        return
    lines.append(f"## {heading}\n")
    for i, entry in enumerate(entries, 1):
        heading = " — ".join(
            part for part in (entry.get("title"), entry.get("company")) if part
        )
        lines.append(f"### {i}. {heading}" if heading else f"### {i}. Job #{entry['job_id']}")
        if entry.get("score") is not None:
            lines.append(f"- **Score:** {entry['score']:.2%}")
        for label, key in (
            ("Location", "location"),
            ("Country", "country"),
            ("Families", "families"),
            ("Remote policy", "remote_policy"),
            ("Seniority", "seniority"),
            ("Salary", "salary"),
            ("Closing date", "closing_date"),
        ):
            value = entry.get(key)
            if value:
                joined = ", ".join(value) if isinstance(value, list) else value
                lines.append(f"- **{label}:** {joined}")
        if entry.get("url"):
            lines.append(f"- **URL:** {entry['url']}")
        lines.append("")
        _append_fit_explanations(lines, entry)


def _append_fit_explanations(lines: list[str], entry: dict) -> None:
    any_value = False
    for label, key in (
        ("Gaps", "gaps"),
        ("Strengths", "strengths"),
        ("Blockers", "blockers"),
    ):
        items = entry.get(key)
        if not items:
            continue
        any_value = True
        lines.append(f"- **{label}:** {', '.join(str(item) for item in items)}")
    if entry.get("summary"):
        any_value = True
        lines.append(f"- **Fit summary:** {entry['summary']}")
    if any_value:
        lines.append("")


def _recommendations_from_jobs(jobs: list[Job], config) -> tuple[dict[str, list[dict]], list[Job]]:
    """Rebuild the grouped recommendations from persisted job rows.

    The report's source of truth when no match run result is passed: it reads
    only what a previous match run wrote and never calls the LLM.

    The group is recovered from the deterministic blocker list plus the score,
    using the same thresholds the match run used (loaded from the same
    defaults config). A blocked job is always ``skip``, so no information is
    lost for jobs that carry a hard blocker; for an unblocked job the group
    follows the configured thresholds, so changing those settings and
    regenerating an old report without re-running match can move a borderline
    job between groups. Re-running match is what fixes that, and re-running is
    always an explicit user action.
    """
    entries: dict[str, list[dict]] = {WORTH: [], CONSIDER: [], SKIP: []}
    unranked: list[Job] = []
    for job in jobs:
        if job.total_score is None:
            continue
        if load_job_profile(job) is None:
            # No structured profile: nothing deterministic can be said about
            # this job, and Phase 4.1 never invents a group for it.
            unranked.append(job)
            continue
        strengths = _parse_json_list(job.fit_strengths)
        gaps = _parse_json_list(job.fit_gaps)
        blockers = _parse_json_list(job.fit_blockers)
        entry = {
            "job_id": job.id,
            "title": job.raw_title,
            "company": job.raw_company,
            "url": job.url,
            "score": job.total_score,
            "location": job.raw_location,
            "country": job.country,
            "families": _parse_json_list(job.included_families),
            "remote_policy": job.remote_policy,
            "seniority": job.seniority,
            "salary": job.salary,
            "closing_date": job.closing_date,
            "strengths": strengths,
            "gaps": gaps,
            "blockers": blockers,
            "summary": job.fit_summary,
        }
        entries[classify_for_report(job.total_score, bool(blockers), config)].append(entry)

    for group in entries.values():
        group.sort(key=lambda item: item["score"], reverse=True)
    return entries, unranked


def classify_for_report(score: float, has_blockers: bool, config) -> str:
    """Map a persisted score + blocker flag back to its recommendation group."""
    if has_blockers:
        return SKIP
    if score >= config.worth_threshold:
        return WORTH
    if score >= config.consider_threshold:
        return CONSIDER
    return SKIP


def _parse_json_list(raw: str | list | None) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []
