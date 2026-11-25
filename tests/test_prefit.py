"""3.8c: prefit ranking, bounded extraction, match status and the job payload."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Job, JobRun, UserProfile
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.profile_evidence import CandidateEvidence
from jobsearch_core.run import extract_top_prefit, match_status
from jobsearch_core.scoring.prefit import prefit_unprofiled


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


def _evidence(**overrides) -> CandidateEvidence:
    base = dict(skills=["python", "fastapi"], target_countries=["fi"], target_roles=["technology/backend"])
    base.update(overrides)
    return CandidateEvidence(**base)


def _add(url: str, **fields) -> int:
    session = get_session()
    job = Job(
        url=url,
        raw_title=fields.pop("raw_title", "Engineer"),
        raw_company="Acme",
        description=fields.pop("description", "A role."),
        country=fields.pop("country", "fi"),
        status="new",
        **fields,
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()
    return job_id


def _ranked(evidence):
    session = get_session()
    try:
        return prefit_unprofiled(session, evidence)
    finally:
        session.close()


def test_prefit_drops_a_non_target_country_unless_the_text_says_remote(db_url):
    init_db(db_url)
    keep = _add("https://e.com/1", country="fi")
    _add("https://e.com/2", country="de", description="On site in Berlin.")
    remote = _add("https://e.com/3", country="de", description="Fully remote, Europe.")
    result = _ranked(_evidence())
    assert {e.job_id for e in result.ranked} == {keep, remote}
    assert result.dropped == 1


def test_prefit_without_target_countries_drops_nothing(db_url):
    init_db(db_url)
    _add("https://e.com/1", country="de")
    assert len(_ranked(_evidence(target_countries=[])).ranked) == 1


def test_prefit_orders_flag_then_role_title_then_skill_hits(db_url):
    init_db(db_url)
    plain = _add("https://e.com/1", description="Nothing relevant here.")
    skills = _add("https://e.com/2", description="We use Python and FastAPI daily.")
    role = _add("https://e.com/3", raw_title="Backend Developer", description="Some role.")
    flagged = _add(
        "https://e.com/4",
        raw_title="Senior Backend Developer",
        description="Python and FastAPI.",
        fit_flags=json.dumps([{"reason": "explicit_senior_title", "detail": "title"}]),
    )
    order = [e.job_id for e in _ranked(_evidence()).ranked]
    assert order == [role, skills, plain, flagged]


def test_prefit_counts_a_typed_role_in_the_title(db_url):
    init_db(db_url)
    plain = _add("https://e.com/1", description="Nothing relevant here.")
    typed = _add("https://e.com/2", raw_title="Solutions Consultant", description="Some role.")
    evidence = _evidence(target_roles=[], custom_roles=[{"text": "solutions consultant", "ids": []}])
    assert [e.job_id for e in _ranked(evidence).ranked] == [typed, plain]


def test_prefit_ranks_a_suspected_non_posting_after_every_real_job(db_url):
    init_db(db_url)
    # Skill-rich page, but its URL and title carry no posting signal.
    page = _add(
        "https://acme.com/en/services/ai",
        raw_title="Artificial Intelligence",
        description="Python FastAPI. " * 40,
        included_families=json.dumps(["backend_python"]),
    )
    real = _add(
        "https://acme.com/jobs/48211",
        raw_title="Backend Developer",
        description="Some role.",
    )
    result = _ranked(_evidence())
    assert [e.job_id for e in result.ranked] == [real, page]
    assert [e.suspect for e in result.ranked] == [False, True]


def test_prefit_rank_is_skill_density_not_a_count_that_grows_with_length(db_url):
    init_db(db_url)
    padding = "word " * 2000
    long_page = _add(
        "https://acme.com/jobs/10001",
        description=f"Python and FastAPI. {padding}",
    )
    short_post = _add(
        "https://acme.com/jobs/10002",
        description="Python and FastAPI in a short, focused posting.",
    )
    order = [e.job_id for e in _ranked(_evidence()).ranked]
    assert order == [short_post, long_page]


def test_prefit_skips_profiled_and_failed_jobs(db_url):
    init_db(db_url)
    _add("https://e.com/1", job_profile=json.dumps({"job_group": "other", "job_role": "other"}))
    _add("https://e.com/2", profile_failed_at=datetime(2026, 1, 1))
    assert _ranked(_evidence()).ranked == []


def test_prefit_takes_a_profile_from_before_the_role_taxonomy(db_url):
    """MF3: a stored profile without job_role is extracted again."""
    init_db(db_url)
    stale = _add("https://e.com/1", job_profile=json.dumps({"required_technologies": []}))
    assert [e.job_id for e in _ranked(_evidence()).ranked] == [stale]


def test_extraction_takes_only_the_top_n_and_reports_the_rest(db_url):
    init_db(db_url)
    ids = [_add(f"https://e.com/{i}") for i in range(5)]
    seen = {}

    def fake_extract(*, settings, jobs, limit, echo=None, retry_failed=False):
        seen["ids"] = [job.id for job in jobs]
        session = get_session()
        for job in jobs:
            session.get(Job, job.id).job_profile = json.dumps({"job_role": "other"})
        session.commit()
        session.close()
        return len(jobs)

    with patch("jobsearch_core.run.extract_missing_profiles", fake_extract):
        extracted, remaining = extract_top_prefit(
            settings=None, evidence=_evidence(), max_extractions=2
        )
    assert seen["ids"] == ids[:2]
    assert (extracted, remaining) == (2, 3)


def test_match_status_flags_a_profile_edited_after_the_last_run(db_url):
    init_db(db_url)
    session = get_session()
    session.add(UserProfile(updated_at=datetime(2026, 11, 1)))
    session.add(
        JobRun(run_type="match", status="completed", params="{}", finished_at=datetime(2026, 11, 2))
    )
    session.commit()
    assert match_status()["profile_changed"] is False

    session.query(UserProfile).update({"updated_at": datetime(2026, 11, 2) + timedelta(hours=1)})
    session.commit()
    session.close()
    assert match_status()["profile_changed"] is True


def test_jobs_endpoint_carries_fit_details_and_orders_by_score(api_client):
    session = get_session()
    session.add_all(
        [
            Job(
                url="https://e.com/low", raw_title="Low", raw_company="A", status="new",
                total_score=0.2, fit_blockers="[]", fit_components=json.dumps({"location": 1.0}),
            ),
            Job(
                url="https://e.com/high", raw_title="High", raw_company="A", status="new",
                total_score=0.9, fit_strengths=json.dumps(["Python"]), fit_gaps="[]",
                fit_blockers="[]", fit_summary="90% match",
                fit_components=json.dumps({"required_technologies": 1.0}),
            ),
            Job(
                url="https://e.com/blocked", raw_title="Blocked", raw_company="A", status="new",
                total_score=0.95, fit_blockers=json.dumps(["needs permit"]),
            ),
        ]
    )
    session.commit()
    session.close()

    rows = api_client.get("/jobs", params={"scored": "true", "order": "score_desc"}).json()
    assert [r["url"].rsplit("/", 1)[1] for r in rows] == ["blocked", "high", "low"]
    by_url = {r["url"].rsplit("/", 1)[1]: r for r in rows}
    assert by_url["high"]["recommendation"] == "worth"
    assert by_url["high"]["fit_strengths"] == ["Python"]
    assert by_url["high"]["fit_components"] == {"required_technologies": 1.0}
    assert by_url["blocked"]["recommendation"] == "skip"
    assert by_url["low"]["recommendation"] == "skip"


def test_suspect_reason_flags_pages_and_keeps_real_postings():
    from jobsearch_core.ingest.scrapers.posting_check import suspect_reason

    assert suspect_reason("Data privacy", "https://digia.com/en/legal-and-privacy")
    assert suspect_reason("Press releaseF-Secure news", "https://f-secure.com/en/newsroom/x")
    # A posting id in the query string is a posting signal (Bittium).
    assert (
        suspect_reason("Senior Full Stack Developer", "https://b.rekrytointi.com/paikat/?jid=216")
        is None
    )
    # An ATS board linked from an employer page is not "off domain" for a stored row.
    assert suspect_reason("Staff Engineer", "https://jobs.ashbyhq.com/cerebras/6a5f5797-64c0") != (
        "off_domain"
    )
    assert suspect_reason("Engineer", "not a url") is None


def test_jobs_endpoint_marks_a_suspect_row(api_client):
    session = get_session()
    session.add_all(
        [
            Job(url="https://e.com/en/services/ai", raw_title="Artificial Intelligence",
                raw_company="A", status="new", total_score=0.82),
            Job(url="https://e.com/jobs/48211", raw_title="Backend Developer",
                raw_company="A", status="new", total_score=0.5),
        ]
    )
    session.commit()
    session.close()
    rows = {r["url"].rsplit("/", 1)[1]: r for r in api_client.get("/jobs").json()}
    assert rows["ai"]["suspect_reason"] == "no_posting_signal"
    assert rows["48211"]["suspect_reason"] is None


def test_matches_status_endpoint(api_client):
    body = api_client.get("/matches/status").json()
    assert body["profile_changed"] is False
    assert body["unprofiled_remaining"] == 0


def test_match_status_results_current_only_after_a_run_started_after_the_save(db_url):
    """MF5: Matches shows results only when a run read the last saved information."""
    init_db(db_url)
    session = get_session()
    session.add(UserProfile(updated_at=datetime(2026, 11, 2, 12)))
    session.commit()
    assert match_status()["results_current"] is False  # no run yet

    # Started before the save, finished after it: it read the old information.
    session.add(
        JobRun(
            run_type="match", status="completed", params="{}",
            started_at=datetime(2026, 11, 2, 11), finished_at=datetime(2026, 11, 2, 13),
        )
    )
    session.commit()
    status = match_status()
    assert status["results_current"] is False
    assert status["profile_changed"] is True

    session.add(
        JobRun(
            run_type="match", status="completed", params="{}",
            started_at=datetime(2026, 11, 2, 14), finished_at=datetime(2026, 11, 2, 15),
        )
    )
    session.commit()
    session.close()
    status = match_status()
    assert status["results_current"] is True
    assert status["profile_changed"] is False


def test_jobs_endpoint_carries_the_hidden_reason(api_client):
    session = get_session()
    session.add(
        Job(
            url="https://e.com/jobs/1", raw_title="Licensing Manager", raw_company="A",
            status="new", total_score=0.3, fit_blockers="[]",
            fit_hidden_reason="Role is Legal & compliance › Licensing & IP, not in your target roles",
        )
    )
    session.commit()
    session.close()
    row = api_client.get("/jobs").json()[0]
    assert row["fit_hidden_reason"].startswith("Role is Legal")
