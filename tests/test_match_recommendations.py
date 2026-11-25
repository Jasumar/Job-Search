"""Phase 4.1 acceptance tests: deterministic profile-vs-evidence matching.

The properties under test, in the order they are stated in the phase plan:

1. Ranking is deterministic and offline: the score is a pure function of the
   persisted ``job_profile`` and the DB evidence, and never of an LLM call.
2. Every job lands in exactly one of worth / consider / skip.
3. A hard blocker forces `skip` no matter how high the score is.
4. A job with no structured profile is *pending*: never scored, never
   presented as a rejected recommendation.
5. Thresholds are inclusive at the boundary.
6. The optional LLM notes pass is bounded and cached per job.
7. Re-scoring is explicit; a second match run does not silently re-rank.
8. Applying stays behind the `tailor` approval gate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import click
import pytest
from jobsearch_core.config import Settings, load_defaults
from jobsearch_core.db.models import Application, Job
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.profile_evidence import (
    CandidateEvidence,
    EvidenceExperience,
    EvidenceLanguage,
)
from jobsearch_core.scoring.match import (
    CONSIDER,
    SKIP,
    WORTH,
    MatchConfig,
    MatchResult,
    classify,
    group_results,
    load_job_profile,
    match_config_from_defaults,
    score_job_against_evidence,
    select_jobs_for_notes,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _evidence(**overrides) -> CandidateEvidence:
    base = dict(
        skills=["python", "fastapi", "postgresql", "docker"],
        experience=[
            EvidenceExperience(
                title="Backend Developer",
                company="Example Oy",
                start="2019-01",
                end="2026-01",
                responsibilities=["built APIs"],
                technologies=["python", "fastapi", "postgresql", "docker"],
            )
        ],
        languages=[EvidenceLanguage(language="English", proficiency="Native")],
        target_countries=["FI"],
        visa_required=False,
        remote_only=False,
    )
    base.update(overrides)
    return CandidateEvidence(**base)


def _job(description: str = "A backend role.", country: str = "FI", **kwargs):
    return SimpleNamespace(
        id=kwargs.pop("id", 1),
        raw_title=kwargs.pop("raw_title", "Backend Developer"),
        raw_company=kwargs.pop("raw_company", "Example Oy"),
        description=description,
        country=country,
        total_score=None,
        status=kwargs.pop("status", "pending"),
        job_profile=kwargs.pop("job_profile", None),
        fit_strengths=None,
        fit_gaps=None,
        fit_blockers=None,
        fit_summary=None,
    )


def _seed_job(**overrides):
    """Insert one job row, filling the NOT NULL url column."""
    fields = {
        "url": overrides.pop("url", "https://example.com/job/1"),
        "raw_title": overrides.pop("raw_title", "Backend"),
        "raw_company": overrides.pop("raw_company", "Example"),
        "description": overrides.pop("description", "text"),
        "country": overrides.pop("country", "FI"),
        "status": overrides.pop("status", "pending"),
    }
    if "job_profile" in overrides:
        fields["job_profile"] = overrides.pop("job_profile")
    fields.update(overrides)
    session = get_session()
    job = Job(**fields)
    session.add(job)
    session.commit()
    # Refresh before closing: `commit()` expires every attribute, so a closed
    # (detached) instance would raise on any later read. Existing tests read
    # `.id` off the return value, so the instance must leave this helper usable.
    session.refresh(job)
    session.close()
    return job


def _patch_match_env():
    """Neutral network + real defaults for the match orchestration tests."""
    return (
        patch("jobsearch_core.run.check_link_liveness"),
        patch("jobsearch_core.run.load_defaults", side_effect=lambda: load_defaults()),
    )


def _strong_profile(**overrides) -> dict:
    profile = {
        "required_technologies": ["python", "fastapi", "postgresql"],
        "nice_to_have_technologies": ["docker"],
        "required_years_of_experience": 3,
        "remote_policy": "remote",
        "sponsorship": "mentioned",
        "languages": [{"language": "English"}],
    }
    profile.update(overrides)
    return profile


# ---------------------------------------------------------------------------
# invariant 1: deterministic and offline
# ---------------------------------------------------------------------------


def test_scoring_is_pure_function_of_profile_and_evidence():
    """Same inputs, same score — no randomness, no clock, no LLM."""
    job = _job()
    profile = _strong_profile()
    first = score_job_against_evidence(job, profile, _evidence())
    second = score_job_against_evidence(job, profile, _evidence())
    assert first.score == second.score
    assert first.recommendation == second.recommendation
    assert first.components == second.components


def test_scoring_never_constructs_an_llm_client():
    """The scorer module has no LLM import in its scoring path."""
    with patch("jobsearch_core.scoring.match.LLMClient") as client:
        score_job_against_evidence(_job(), _strong_profile(), _evidence())
    client.assert_not_called()


def test_weights_are_normalised_over_applied_components():
    """A profile stating no requirements still yields a comparable 0..1."""
    result = score_job_against_evidence(_job(), {}, _evidence())
    assert 0.0 <= result.score <= 1.0
    assert isinstance(result.recommendation, str)


def test_extra_evidence_raises_the_score():
    job, profile = _job(), _strong_profile()
    lean = score_job_against_evidence(
        job, profile, _evidence(skills=["python"], experience=[], languages=[])
    )
    strong = score_job_against_evidence(job, profile, _evidence())
    assert strong.score > lean.score


# ---------------------------------------------------------------------------
# invariant 2 and 5: one group, inclusive boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (1.00, WORTH),
        (0.70, WORTH),  # inclusive lower bound of worth
        (0.6999, CONSIDER),
        (0.40, CONSIDER),  # inclusive lower bound of consider
        (0.3999, SKIP),
        (0.00, SKIP),
    ],
)
def test_threshold_boundaries(score, expected):
    assert classify(score, []) == expected


def test_every_job_lands_in_exactly_one_group():
    groups = group_results(
        [
            MatchResult(job_id=1, score=0.9, recommendation=WORTH),
            MatchResult(job_id=2, score=0.5, recommendation=CONSIDER),
            MatchResult(job_id=3, score=0.1, recommendation=SKIP),
        ]
    )
    assert [len(groups[name]) for name in (WORTH, CONSIDER, SKIP)] == [1, 1, 1]
    assert sum(len(v) for v in groups.values()) == 3


def test_groups_are_sorted_by_score_desc_then_job_id():
    groups = group_results(
        [
            MatchResult(job_id=9, score=0.5, recommendation=CONSIDER),
            MatchResult(job_id=3, score=0.5, recommendation=CONSIDER),
            MatchResult(job_id=7, score=0.9, recommendation=CONSIDER),
        ]
    )
    assert [r.job_id for r in groups[CONSIDER]] == [7, 3, 9]


# ---------------------------------------------------------------------------
# invariant 3: blockers dominate
# ---------------------------------------------------------------------------


def test_blockers_force_skip_even_at_a_perfect_score():
    assert classify(1.0, ["no sponsorship"]) == SKIP


def test_blocked_job_is_skip_even_when_everything_matches():
    job = _job(description="You must hold a valid Finnish work permit.")
    result = score_job_against_evidence(job, _strong_profile(), _evidence())
    assert result.recommendation == SKIP
    assert result.blockers


def test_visa_required_without_sponsorship_is_a_blocker():
    job = _job()
    profile = _strong_profile(sponsorship="not_stated")
    evidence = _evidence(visa_required=True)
    result = score_job_against_evidence(job, profile, evidence)
    assert result.recommendation == SKIP
    assert any("visa" in b.lower() for b in result.blockers)


def test_mentioned_sponsorship_clears_the_visa_blocker():
    job = _job()
    evidence = _evidence(visa_required=True)
    result = score_job_against_evidence(job, _strong_profile(sponsorship="mentioned"), evidence)
    assert not any("visa" in b.lower() for b in result.blockers)


def test_onsite_role_outside_target_countries_is_a_blocker():
    job = _job(country="DE")
    result = score_job_against_evidence(
        job, _strong_profile(remote_policy="onsite"), _evidence()
    )
    assert result.recommendation == SKIP
    assert any("target country" in b for b in result.blockers)


def test_remote_role_ignores_the_job_country():
    job = _job(country="DE")
    result = score_job_against_evidence(
        job, _strong_profile(remote_policy="remote"), _evidence()
    )
    assert not any("target country" in b for b in result.blockers)


# ---------------------------------------------------------------------------
# invariant 4: a missing profile is pending, not rejected
# ---------------------------------------------------------------------------


def test_load_job_profile_returns_none_when_absent():
    assert load_job_profile(_job(job_profile=None)) is None


def test_load_job_profile_returns_none_on_unparseable_json():
    assert load_job_profile(_job(job_profile="{not json")) is None


def test_load_job_profile_returns_none_on_non_object_json():
    assert load_job_profile(_job(job_profile="[1, 2, 3]")) is None


def test_load_job_profile_parses_a_persisted_profile():
    parsed = load_job_profile(_job(job_profile=json.dumps(_strong_profile())))
    assert parsed is not None
    assert parsed["remote_policy"] == "remote"


def test_missing_profile_is_pending_not_scored(tmp_path):
    """A job with no profile gets no score and lands in no group."""
    from jobsearch_core.run import match_jobs

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _seed_job(job_profile=None)

    live, defaults = _patch_match_env()
    with live, defaults:
        summary = match_jobs(settings=Settings(), evidence=_evidence())

    assert summary.jobs_pending == 1
    assert summary.jobs_scored == 0
    assert sum(len(v) for v in summary.recommendations.values()) == 0

    session = get_session()
    assert session.query(Job).one().total_score is None
    session.close()


# ---------------------------------------------------------------------------
# invariant 6: bounded, cached notes
# ---------------------------------------------------------------------------


def test_notes_selection_is_worth_first_then_consider():
    results = [
        MatchResult(job_id=1, score=0.5, recommendation=CONSIDER),
        MatchResult(job_id=2, score=0.1, recommendation=SKIP),
        MatchResult(job_id=3, score=0.9, recommendation=WORTH),
    ]
    assert [r.job_id for r in select_jobs_for_notes(results)] == [3, 1]


def test_notes_never_include_skipped_jobs():
    results = [MatchResult(job_id=i, score=0.1, recommendation=SKIP) for i in range(5)]
    assert select_jobs_for_notes(results) == []


def test_notes_are_bounded_by_the_budget(tmp_path):
    from jobsearch_core.run import match_jobs

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    for i in range(4):
        _seed_job(
            url=f"https://example.com/job/{i}",
            job_profile=json.dumps(_strong_profile()),
        )

    live, defaults = _patch_match_env()

    def _real_defaults():
        real = load_defaults()
        real.max_llm_notes = 2
        return real

    with live, patch("jobsearch_core.run._apply_llm_notes", return_value=1) as notes, patch(
        "jobsearch_core.run.load_defaults", side_effect=_real_defaults
    ):
        match_jobs(settings=Settings(), evidence=_evidence(), llm_notes=True)

    notes.assert_called_once()
    assert notes.call_args.kwargs["budget"] == 2


def test_notes_are_off_by_default(tmp_path):
    from jobsearch_core.run import match_jobs

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _seed_job(job_profile=json.dumps(_strong_profile()))

    live, defaults = _patch_match_env()
    with live, patch("jobsearch_core.run._apply_llm_notes") as notes, defaults:
        summary = match_jobs(settings=Settings(), evidence=_evidence())

    notes.assert_not_called()
    assert summary.notes_written == 0


def test_notes_cache_hit_makes_no_api_call(tmp_path):
    """A cached prompt returns from disk without touching the client."""
    from jobsearch_core.llm.cache import save_cached
    from jobsearch_core.scoring.match import (
        MATCH_NOTES_SYSTEM,
        build_match_notes_prompt,
        explain_match,
    )

    profile = _strong_profile()
    payload = {"skills": ["python"]}
    prompt = build_match_notes_prompt(profile, payload)
    save_cached(
        tmp_path,
        MATCH_NOTES_SYSTEM,
        prompt,
        json.dumps({"summary": "cached", "strengths": [], "gaps": [], "blockers": []}),
    )

    client = SimpleNamespace(
        complete_json=SimpleNamespace(side_effect=AssertionError("must not be called"))
    )
    result = explain_match(client, tmp_path, profile, payload)
    assert result["summary"] == "cached"


def test_notes_cannot_revise_a_deterministic_recommendation(tmp_path):
    """LLM notes may never move a job between recommendation groups.

    The notes pass explains the deterministic outcome, it does not re-decide it.
    `report.classify_for_report` re-derives the group from persisted
    (score, has_blockers) alone, so any blocker the model invents would silently
    demote a Worth job to Skip in a report regenerated later — the same job in
    two different groups depending on when the report was built.
    """
    from jobsearch_core.report import classify_for_report
    from jobsearch_core.run import _apply_llm_notes
    from jobsearch_core.scoring.match import MatchConfig, MatchResult

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    seeded = _seed_job(
        job_profile=json.dumps(_strong_profile()),
        total_score=0.95,
        fit_blockers=json.dumps([]),
    )
    job_id = seeded.id
    deterministic = MatchResult(
        job_id=job_id,
        score=0.95,
        recommendation=WORTH,
        blockers=[],
        strengths=["Python in evidence"],
    )
    assert classify_for_report(0.95, False, MatchConfig()) == WORTH

    # The model wrongly reports a blocker on an unblocked job.
    notes = {
        "summary": "Strong match overall.",
        "strengths": ["Python in evidence"],
        "gaps": [],
        "blockers": ["Requires security clearance the candidate lacks"],
    }
    session = get_session()
    try:
        with patch("jobsearch_core.run.explain_match", return_value=notes), patch(
            "jobsearch_core.run.LLMClient"
        ):
            written = _apply_llm_notes(
                session=session,
                settings=Settings(),
                results=[deterministic],
                evidence=_evidence(),
                budget=10,
            )
        assert written == 1
        stored = session.get(Job, job_id)
        # The invented blocker is dropped: the row still reads back as Worth.
        assert json.loads(stored.fit_blockers) == []
        assert classify_for_report(stored.total_score, bool(json.loads(stored.fit_blockers)), MatchConfig()) == WORTH
        # Narrative fields were still replaced by the notes.
        assert stored.fit_summary == notes["summary"]
        assert json.loads(stored.fit_strengths) == notes["strengths"]
    finally:
        session.close()


def test_empty_database_returns_all_three_empty_groups(tmp_path):
    """An empty run still exposes every group, so callers need no special case."""
    from jobsearch_core.run import match_jobs
    from jobsearch_core.scoring.match import RECOMMENDATION_ORDER

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    live, defaults = _patch_match_env()
    with live, defaults:
        summary = match_jobs(settings=Settings(), evidence=_evidence())

    assert summary.jobs_found == 0
    assert summary.jobs_scored == 0
    assert summary.recommendations == {name: [] for name in RECOMMENDATION_ORDER}


# ---------------------------------------------------------------------------
# 3.8c M3: every run rescores every profiled job
# ---------------------------------------------------------------------------


def test_every_match_run_rescores_profiled_jobs(tmp_path):
    from jobsearch_core.run import match_jobs

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _seed_job(job_profile=json.dumps(_strong_profile()))

    def _run():
        live, defaults = _patch_match_env()
        with live, defaults:
            return match_jobs(settings=Settings(), evidence=_evidence())

    first = _run()
    assert first.jobs_scored == 1
    first_score = _stored_score()

    # A changed job profile gives a different number on the next run, with no flag.
    session = get_session()
    job = session.query(Job).one()
    job.job_profile = json.dumps(_strong_profile(required_technologies=["cobol", "fortran"]))
    session.commit()
    session.close()

    second = _run()
    assert second.jobs_scored == 1
    assert _stored_score() != first_score


def _stored_score() -> float:
    session = get_session()
    try:
        return session.query(Job).one().total_score
    finally:
        session.close()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_match_config_defaults_match_the_plan():
    config = MatchConfig()
    assert config.worth_threshold == 0.70
    assert config.consider_threshold == 0.40
    assert config.max_llm_notes == 10


def test_thresholds_are_configurable():
    config = MatchConfig(worth_threshold=0.9, consider_threshold=0.8)
    assert classify(0.85, [], config) == CONSIDER
    assert classify(0.95, [], config) == WORTH


def test_defaults_yaml_declares_the_match_keys():
    defaults = load_defaults()
    assert defaults.worth_threshold == 0.70
    assert defaults.consider_threshold == 0.40
    assert defaults.max_llm_notes == 10
    assert defaults.max_profile_extractions >= 1


def test_match_config_from_defaults_reads_the_thresholds():
    config = match_config_from_defaults(load_defaults())
    assert config.worth_threshold == load_defaults().worth_threshold
    assert config.consider_threshold == load_defaults().consider_threshold


def test_defaults_never_enable_llm_notes():
    """Notes are opt-in per invocation; a default config cannot turn them on."""
    assert not hasattr(load_defaults(), "llm_notes")


# ---------------------------------------------------------------------------
# persistence: reuse existing columns, no migration
# ---------------------------------------------------------------------------


def test_result_is_written_into_the_reused_columns():
    from jobsearch_core.scoring.match import apply_result_to_job

    job = _job()
    result = score_job_against_evidence(job, _strong_profile(), _evidence())
    apply_result_to_job(job, result)
    assert job.total_score == pytest.approx(result.score)
    assert json.loads(job.fit_strengths) == result.strengths
    assert json.loads(job.fit_gaps) == result.gaps
    assert json.loads(job.fit_blockers) == result.blockers
    assert job.fit_summary == result.summary


# ---------------------------------------------------------------------------
# report output
# ---------------------------------------------------------------------------


def _entry(job_id: int, score: float, summary: str, **extra) -> dict:
    entry = {
        "job_id": job_id,
        "title": f"Role {job_id}",
        "company": f"Company {job_id}",
        "url": f"https://example.com/job/{job_id}",
        "score": score,
        "summary": summary,
        "blockers": [],
    }
    entry.update(extra)
    return entry


def test_report_renders_the_three_groups(tmp_path):
    from jobsearch_core.report import generate_report

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    out = tmp_path / "report.md"
    generate_report(
        jobs=[],
        output_path=str(out),
        recommendations={
            WORTH: [_entry(1, 0.9, "great fit")],
            CONSIDER: [_entry(2, 0.5, "partial")],
            SKIP: [_entry(3, 0.1, "poor", blockers=["blocked"])],
        },
    )
    text = out.read_text(encoding="utf-8")
    assert "Worth Applying" in text
    assert "Consider" in text
    assert "Skip" in text
    assert "great fit" in text
    assert "poor" in text
    assert "Role 1 — Company 1" in text


def test_report_renders_a_bare_entry_without_presentation_fields(tmp_path):
    """A results-only payload must not crash the renderer."""
    from jobsearch_core.report import generate_report

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    out = tmp_path / "report.md"
    generate_report(
        jobs=[],
        output_path=str(out),
        recommendations={WORTH: [{"job_id": 7, "score": 0.8, "summary": "ok"}]},
    )
    text = out.read_text(encoding="utf-8")
    assert "Job #7" in text
    assert "80.00%" in text


def test_report_omits_the_retired_ranked_jobs_heading(tmp_path):
    from jobsearch_core.report import generate_report

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    out = tmp_path / "report.md"
    generate_report(
        jobs=[], output_path=str(out), recommendations={WORTH: [], CONSIDER: [], SKIP: []}
    )
    assert "Ranked Jobs" not in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# invariant 8: applying stays behind the approval gate
# ---------------------------------------------------------------------------


def test_match_does_not_write_an_application(tmp_path):
    """Matching is read + score only; applying is the separate tailor step."""
    from jobsearch_core.run import match_jobs

    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _seed_job(job_profile=json.dumps(_strong_profile()))

    live, defaults = _patch_match_env()
    with live, defaults:
        match_jobs(settings=Settings(), evidence=_evidence())

    session = get_session()
    assert session.query(Job).one().status == "pending"
    assert session.query(Application).count() == 0
    session.close()


def test_tailor_gate_is_a_flag_not_a_default(runner):
    """--yes only skips the prompt; it is not on by default.

    The sentinel the option carries is Click bookkeeping, so the default is
    asserted behaviourally by the next test rather than by introspection.
    """
    from jobsearch_core import cli

    yes_option = next(
        opt for opt in cli.main.commands["tailor"].params if opt.name == "yes"
    )
    assert isinstance(yes_option, click.Option)
    assert yes_option.is_flag is True
    assert "Skip approval prompt" in str(yes_option.help)


def test_declining_the_approval_discards_the_tailored_cv(runner, tmp_path, monkeypatch):
    """The gate is real: answering "n" writes no application."""
    from jobsearch_core import cli

    monkeypatch.chdir(tmp_path)
    init_db(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _seed_job(description="A backend role.")

    with patch("jobsearch_core.cli.init_db"), patch(
        "jobsearch_core.cli.tailor_cv"
    ) as tailor_cv, patch("jobsearch_core.cli.LLMClient"):
        tailor_cv.return_value = SimpleNamespace(
            diff="- a\n+ b", tailored="tailored", cache_hit=False
        )
        result = runner.invoke(cli.main, ["tailor", "1", "--variant", "backend"], input="n\n")

    assert "Tailoring discarded." in result.output
    assert not (tmp_path / "data" / "applications").exists()

    session = get_session()
    assert session.query(Application).count() == 0
    session.close()
