from __future__ import annotations

import json

import pytest
from jobsearch_core.db.models import Job
from jobsearch_core.db.session import get_engine, get_session, init_db
from jobsearch_core.run import check_link_liveness, extract_missing_profiles
from jobsearch_core.scoring.profile import (
    JobProfileResponse,
    populate_flattened_from_profile,
)
from sqlalchemy import inspect

LEGACY_JOBS_SQL = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    authority_url TEXT,
    extraction_url TEXT,
    source_url TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    verified_at DATETIME,
    raw_title TEXT NOT NULL,
    raw_company TEXT NOT NULL,
    raw_location TEXT,
    normalized_title TEXT,
    normalized_company TEXT,
    normalized_location TEXT,
    country TEXT,
    description TEXT,
    posted_date TEXT,
    closing_date TEXT,
    remote_policy TEXT,
    employment_type TEXT,
    seniority TEXT,
    languages_required TEXT,
    work_authorization TEXT,
    sponsorship TEXT,
    salary TEXT,
    extracted_requirements TEXT,
    dedup_hash TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    rejected_by TEXT,
    rejected_reason TEXT,
    included_families TEXT,
    include_signals TEXT,
    total_score REAL,
    fit_gaps TEXT,
    fit_strengths TEXT,
    fit_blockers TEXT,
    fit_summary TEXT,
    discovered_at DATETIME,
    updated_at DATETIME
)
"""


def _create_pre_36d_db(url: str) -> None:
    """A Phase 3.6c-era jobs table without the 3.6d columns."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(LEGACY_JOBS_SQL))
        conn.execute(
            text(
                "INSERT INTO jobs (url, raw_title, raw_company, description, status) "
                "VALUES (:url, :title, :company, :description, 'new')"
            ),
            {"url": "https://x.example/j1", "title": "Dev", "company": "X",
             "description": "Python backend role."},
        )
    engine.dispose()


def test_migration_adds_36d_columns_to_legacy_db(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'legacy36d.db'}"
    _create_pre_36d_db(db_url)

    init_db(db_url)
    inspector = inspect(get_engine())
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    assert {"job_profile", "link_checked_at", "link_ok"} <= columns

    session = get_session()
    job = session.query(Job).one()
    session.close()
    assert job.job_profile is None
    assert job.link_checked_at is None
    assert job.link_ok is None
    get_engine().dispose()


def test_profile_response_defaults():
    profile = JobProfileResponse()
    data = profile.model_dump()
    assert data["required_technologies"] == []
    assert data["seniority"] == "unknown"
    assert data["sponsorship"] == "not_stated"
    assert data["remote_policy"] == "unknown"


def test_populate_flattened_from_profile_fills_legacy_columns():
    job = Job(url="https://x.example/j", raw_title="T", raw_company="C")
    profile = {
        "languages": [{"language": "Finnish", "level": "B2"}],
        "remote_policy": "hybrid",
        "seniority": "senior",
        "employment_type": "permanent",
        "salary": "60k",
        "closing_date": "2026-10-01",
        "sponsorship": "mentioned",
    }
    populate_flattened_from_profile(job, profile)
    assert json.loads(job.languages_required) == ["Finnish B2"]
    assert job.remote_policy == "hybrid"
    assert job.seniority == "senior"
    assert job.employment_type == "permanent"
    assert job.salary == "60k"
    assert job.closing_date == "2026-10-01"
    assert job.sponsorship == "mentioned"


def test_populate_flattened_does_not_clobber_with_unknowns():
    job = Job(url="https://x.example/j", raw_title="T", raw_company="C")
    populate_flattened_from_profile(job, {
        "languages": [], "remote_policy": "unknown", "seniority": "unknown",
        "sponsorship": "not_stated",
    })
    assert job.languages_required is None
    assert job.remote_policy is None
    assert job.seniority is None
    assert job.sponsorship == "not_stated"


class _StubSettings:
    llm_cache_dir = None  # unused by the stub client path
    openrouter_api_key = ""
    analysis_model = "stub"
    llm_base_url = None
    llm_min_interval_seconds = 0.0
    llm_max_retries = 0


@pytest.fixture()
def jobs_db(tmp_path):
    """init_db on a fresh DB."""
    db_url = f"sqlite:///{tmp_path / 'jobs.db'}"
    init_db(db_url)
    yield db_url
    get_engine().dispose()


def _add_job(**kwargs) -> int:
    session = get_session()
    defaults = dict(url="https://x.example/j", raw_title="T", raw_company="C",
                    description="A Python backend role.")
    defaults.update(kwargs)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()
    return job_id


def test_extract_missing_profiles_backfills_and_skips_existing(jobs_db, monkeypatch):
    job_no_profile = _add_job(url="https://x.example/a")
    job_scored = _add_job(url="https://x.example/b", total_score=0.9)
    _add_job(url="https://x.example/c", description=None)
    already = _add_job(url="https://x.example/d", job_profile='{"summary": "x", "job_role": "other"}')

    calls = []

    def fake_extract(client, cache_dir, description, country=None):
        calls.append(description)
        return {"summary": "profiled", "languages": [], "job_group": "other", "job_role": "other"}

    monkeypatch.setattr(
        "jobsearch_core.run.extract_job_profile", fake_extract
    )

    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    extracted = extract_missing_profiles(settings=_StubSettings(), jobs=jobs)

    assert extracted == 2  # backfills scored jobs too, skips profile + no-desc
    session = get_session()
    session.expire_all()
    assert json.loads(session.get(Job, job_no_profile).job_profile)["summary"] == "profiled"
    assert session.get(Job, job_scored).job_profile is not None
    assert session.get(Job, already).job_profile == '{"summary": "x", "job_role": "other"}'
    assert len(calls) == 2
    session.close()

    # Second pass is a no-op (never re-extracted).
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    assert extract_missing_profiles(settings=_StubSettings(), jobs=jobs) == 0


def test_extract_missing_profiles_limit(jobs_db, monkeypatch):
    for i in range(3):
        _add_job(url=f"https://x.example/{i}")
    monkeypatch.setattr(
        "jobsearch_core.run.extract_job_profile",
        lambda *a, **k: {"summary": "p"},
    )
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    assert extract_missing_profiles(settings=_StubSettings(), jobs=jobs, limit=2) == 2


def test_extract_missing_profiles_failure_does_not_block(jobs_db, monkeypatch):
    _add_job(url="https://x.example/ok")
    _add_job(url="https://x.example/bad")

    # Fail only for the first call.
    state = {"n": 0}

    def first_fails(client, cache_dir, description, country=None):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("LLM down")
        return {"summary": "fine"}

    monkeypatch.setattr("jobsearch_core.run.extract_job_profile", first_fails)
    session = get_session()
    jobs = session.query(Job).order_by(Job.id).all()
    session.close()
    extracted = extract_missing_profiles(settings=_StubSettings(), jobs=jobs)
    assert extracted == 1  # the failed job is skipped, the run continues


def test_liveness_marks_jobs_without_network(jobs_db, monkeypatch):
    """Without a real HTTP path, failures mark link_ok=False, not crash."""
    _add_job(url="https://nonexistent-host-3x6d.invalid/j1")
    _add_job(url="https://nonexistent-host-3x6d.invalid/j2")

    class _FailClient:
        _timeout = 0.1

        class _FailSession:
            @staticmethod
            def request(*a, **k):
                raise RuntimeError("no network")

        _session = _FailSession()

        @staticmethod
        def _wait_for_host(host):
            pass

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(
        "jobsearch_core.ingest.http.PoliteClient", lambda **k: _FailClient()
    )
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    checked = check_link_liveness(settings=_StubSettings(), jobs=jobs, max_checks=10)
    assert checked == 2
    session = get_session()
    session.expire_all()
    rows = session.query(Job).all()
    assert all(job.link_ok is False for job in rows)
    assert all(job.link_checked_at is not None for job in rows)
    session.close()


def test_liveness_bounded_and_never_rechecks(jobs_db, monkeypatch):
    for i in range(4):
        _add_job(url=f"https://nonexistent-host-3x6d.invalid/{i}")

    class _FailClient:
        _timeout = 0.1
        _session = type("S", (), {"request": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))})()
        @staticmethod
        def _wait_for_host(host): pass
        @staticmethod
        def close(): pass

    monkeypatch.setattr(
        "jobsearch_core.ingest.http.PoliteClient", lambda **k: _FailClient()
    )
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    assert check_link_liveness(settings=_StubSettings(), jobs=jobs, max_checks=2) == 2
    # Checked jobs are never re-checked; only the 2 unchecked remain.
    assert check_link_liveness(settings=_StubSettings(), jobs=jobs, max_checks=10) == 2
    assert check_link_liveness(settings=_StubSettings(), jobs=jobs, max_checks=10) == 0


def test_liveness_head_fallback_get(monkeypatch, jobs_db):
    """A 405 on HEAD falls back to GET; 2xx marks link_ok=True."""
    _add_job(url="https://ok.example/j")

    class _Resp:
        def __init__(self, status):
            self.status_code = status

    class _Sess:
        @staticmethod
        def request(method, url, **k):
            if method == "HEAD":
                return _Resp(405)
            return _Resp(200)

    class _Client:
        _timeout = 1.0
        _session = _Sess()
        @staticmethod
        def _wait_for_host(host): pass
        @staticmethod
        def close(): pass

    monkeypatch.setattr("jobsearch_core.ingest.http.PoliteClient", lambda **k: _Client())
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    assert check_link_liveness(settings=_StubSettings(), jobs=jobs, max_checks=10) == 1
    session = get_session()
    session.expire_all()
    assert session.query(Job).one().link_ok is True
    session.close()


# --- RF8: failure marker -------------------------------------------------------


class _InvalidLLM:
    """An LLM whose answer fails profile validation."""

    def __init__(self, answer='{"required_years_of_experience": -3}'):
        self.answer = answer
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return self.answer


def test_invalid_answer_is_not_cached(tmp_path):
    from jobsearch_core.scoring.profile import InvalidProfileResponseError, extract_job_profile

    llm = _InvalidLLM()
    with pytest.raises(InvalidProfileResponseError):
        extract_job_profile(llm, tmp_path, "Some description")
    assert list(tmp_path.rglob("*.json")) == []


def test_invalid_cached_answer_is_asked_again(tmp_path):
    from jobsearch_core.llm.cache import save_cached
    from jobsearch_core.scoring.profile import EXTRACT_PROFILE_SYSTEM, extract_job_profile

    save_cached(
        tmp_path, EXTRACT_PROFILE_SYSTEM, "Job description:\n\nSome description",
        '{"required_years_of_experience": -3}',
    )
    llm = _InvalidLLM(answer='{"summary": "ok"}')
    assert extract_job_profile(llm, tmp_path, "Some description")["summary"] == "ok"
    assert llm.calls == 1


def _failing_extract(client, cache_dir, description, country=None):
    from jobsearch_core.scoring.profile import InvalidProfileResponseError

    raise InvalidProfileResponseError("bad")


def test_invalid_answer_marks_job_and_later_runs_skip_it(jobs_db, monkeypatch):
    _add_job(url="https://x.example/bad")
    monkeypatch.setattr("jobsearch_core.run.extract_job_profile", _failing_extract)

    def load():
        s = get_session()
        jobs = s.query(Job).all()
        s.close()
        return jobs

    assert extract_missing_profiles(settings=_StubSettings(), jobs=load()) == 0
    session = get_session()
    assert session.query(Job).one().profile_failed_at is not None
    session.close()

    def must_not_run(*a, **k):
        raise AssertionError("a marked job must be skipped")

    monkeypatch.setattr("jobsearch_core.run.extract_job_profile", must_not_run)
    assert extract_missing_profiles(settings=_StubSettings(), jobs=load()) == 0

    # A manual rerun clears the marker on success.
    monkeypatch.setattr(
        "jobsearch_core.run.extract_job_profile", lambda *a, **k: {"summary": "now valid"}
    )
    assert extract_missing_profiles(settings=_StubSettings(), jobs=load(), retry_failed=True) == 1
    session = get_session()
    job = session.query(Job).one()
    assert job.profile_failed_at is None and job.job_profile is not None
    session.close()


def test_transport_failure_does_not_mark_job(jobs_db, monkeypatch):
    _add_job(url="https://x.example/net")

    def down(*a, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr("jobsearch_core.run.extract_job_profile", down)
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    extract_missing_profiles(settings=_StubSettings(), jobs=jobs)
    session = get_session()
    assert session.query(Job).one().profile_failed_at is None
    session.close()


def test_rejected_jobs_are_not_extracted(jobs_db, monkeypatch):
    rejected = _add_job(url="https://x.example/rej")
    session = get_session()
    session.get(Job, rejected).status = "rejected"
    session.commit()
    session.close()

    def must_not_run(*a, **k):
        raise AssertionError("rejected jobs must not be extracted")

    monkeypatch.setattr("jobsearch_core.run.extract_job_profile", must_not_run)
    session = get_session()
    jobs = session.query(Job).all()
    session.close()
    assert extract_missing_profiles(settings=_StubSettings(), jobs=jobs) == 0
