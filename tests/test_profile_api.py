"""Phase 3.8f backend tests: user_profile, endpoints, profile_parse run.

Decisions under test (master_plan.md "User Profile in DB", 2026-09-11):

- Alembic pre-task: fresh DBs are create_all + stamp head; existing DBs
  replay migrations; migration 0003 adds user_profile.
- GET /profile → 200 with an empty profile when none exists (not 404).
- PUT /profile → full-replace in one atomic write; never touches cv_text.
- POST /profile/cv → 202 {run_id} (run_type profile_parse); raw CV persisted
  at parse time; proposal lands in run summary, never in user_profile.
- GET /segments → chip/segment/country data from tech.yaml, not hardcoded.
- GET/POST /discovery/preview → computed search profile, ceiling lower-only.
- Discovery snapshot: profile is read at run start; mid-run edits never
  affect an in-flight run; DB-first with evidence.yaml fallback.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.session import get_engine, get_session, init_db
from jobsearch_core.profile import (
    ProfileData,
    get_profile_row,
    profile_snapshot,
    replace_profile,
)
from sqlalchemy import inspect


@pytest.fixture
def client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'profile.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client


def _make_profile(tmp_path, **fields):
    database_url = f"sqlite:///{tmp_path / 'profile.db'}"
    monkeypatch_free_init(database_url)
    session = get_session()
    try:
        replace_profile(session, ProfileData(**fields))
    finally:
        session.close()


def monkeypatch_free_init(database_url: str) -> None:
    init_db(database_url)


def _alembic_head() -> str:
    """The single current head revision, read from the script directory.

    Asserting a literal revision here made every new migration break two
    unrelated tests. Reading the head keeps the real invariant under test —
    a fresh/existing DB is brought all the way to head — while still failing
    loudly if the migration graph ever forks into two heads.
    """
    from alembic.script import ScriptDirectory
    from jobsearch_core.db.migrations import _alembic_config

    heads = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_heads()
    assert len(heads) == 1, f"migration graph forked into multiple heads: {heads}"
    return heads[0]


# --- Alembic pre-task -------------------------------------------------------


def test_fresh_db_is_stamped_and_has_user_profile(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    init_db(database_url)
    engine = get_engine()
    inspector = inspect(engine)
    assert "user_profile" in inspector.get_table_names()
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert version == _alembic_head()
    engine.dispose()


def test_existing_db_without_alembic_is_brought_to_head(tmp_path):
    """A pre-Alembic database (like the local data/jobs.db) is stamped at the
    baseline and migrated to head without touching its data."""
    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    init_db(database_url)  # create schema via create_all first
    engine = get_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()

    init_db(database_url)  # second init: legacy DB (no alembic_version)
    engine = get_engine()
    inspector = inspect(engine)
    assert "user_profile" in inspector.get_table_names()
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert version == _alembic_head()
    engine.dispose()


# --- Profile endpoints ------------------------------------------------------


def test_get_profile_empty_is_200(client):
    response = client.get("/profile")
    assert response.status_code == 200
    body = response.json()
    assert body["profile"] is None


def test_put_profile_full_replaces(client):
    response = client.put(
        "/profile",
        json={
            "name": "Test User",
            "education": "BSc CS",
            "experience": "5 years backend",
            "skills": ["Python", "FastAPI"],
            "job_preferences": ["backend roles"],
            "notes": ["EU citizen"],
            "target_countries": ["Finland", "Sweden"],
            "target_cities": ["Helsinki"],
            "role_families": ["backend", "ai"],
            "custom_roles": [{"text": "solutions consultant", "ids": ["sales/pre-sales", "nope/x"]}],
            "work_arrangements": ["full-time", "remote"],
        },
    )
    assert response.status_code == 200
    body = response.json()["profile"]
    assert body["name"] == "Test User"
    assert body["target_countries"] == ["Finland", "Sweden"]
    # MF3: old chip ids are stored as taxonomy ids; unknown ids are dropped.
    assert body["role_families"] == ["technology/backend", "technology/ml-engineer"]
    assert body["custom_roles"] == [{"text": "solutions consultant", "ids": ["sales/pre-sales"]}]
    assert body["work_arrangements"] == ["full-time", "remote"]

    # Full replace: a second PUT without skills empties them.
    response = client.put("/profile", json={"name": "Test User"})
    body = response.json()["profile"]
    assert body["skills"] == []
    assert body["target_countries"] == []
    assert body["role_families"] == []
    assert body["work_arrangements"] == []
    # updated_at is set (profile reference for observability)
    assert body["updated_at"] is not None


def test_put_profile_never_writes_cv_text(client):
    """PUT must not be able to write the raw CV (decided 2026-09-11)."""
    client.put("/profile", json={"name": "A"})
    session = get_session()
    try:
        row = get_profile_row(session)
        assert row.cv_text is None
    finally:
        session.close()


def test_profile_cv_enqueues_profile_parse_run(client):
    response = client.post(
        "/profile/cv",
        json={"cv_text": "Experienced Python developer...", "description": "backend roles"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["run_type"] == "profile_parse"
    assert run["params"]["cv_text"].startswith("Experienced")
    assert run["status"] == "queued"
    # Second concurrent CV parse conflicts (one active run per type).
    assert client.post("/profile/cv", json={"cv_text": "x"}).status_code == 409


def test_get_segments_serves_scope_data(client):
    response = client.get("/segments")
    assert response.status_code == 200
    body = response.json()
    family_ids = [f["id"] for f in body["role_families"]]
    assert "backend" in family_ids
    backend = next(f for f in body["role_families"] if f["id"] == "backend")
    assert backend["label"] and backend["segments"]
    assert "startup" in body["segments"]
    assert "Finland" in body["countries"]


def test_discovery_preview_computed_and_capped(client, tmp_path, monkeypatch):
    from jobsearch_api import routes

    monkeypatch.setattr(routes, "load_defaults", lambda: type(
        "D", (), {"max_baseline_queries": 15}
    )())
    # No profile: falls back to evidence (missing file → all countries),
    # and the cap holds the baseline count at the ceiling.
    response = client.get("/discovery/preview")
    assert response.status_code == 200
    body = response.json()
    assert body["baseline_query_count"] == body["ceiling"]
    assert len(body["sample_queries"]) == body["baseline_query_count"]
    assert body["profile_source"] in ("evidence_yaml", "default")

    # API may lower, never raise: a request above the ceiling is clamped.
    response = client.get("/discovery/preview", params={"max_baseline_queries": 99})
    assert response.json()["ceiling"] == 15
    response = client.get("/discovery/preview", params={"max_baseline_queries": 4})
    assert response.json()["baseline_query_count"] == 4

    # With a DB profile, the preview is profile-driven.
    _make_profile(tmp_path, target_countries=["Finland", "Sweden"])
    response = client.get("/discovery/preview")
    body = response.json()
    assert body["profile_source"] == "db_profile"
    assert body["selected_countries"] == ["Finland", "Sweden"]
    assert body["profile_reference"] is not None


def test_discovery_preview_wizard_selection_narrows_segments(client, tmp_path):
    """Profile role-family chips narrow the segment templates (decided
    2026-09-11: ordered intersection, per-chip cap 3) and record the
    selection for observability."""
    _make_profile(
        tmp_path,
        target_countries=["Finland"],
        role_families=["data"],  # maps to software, fintech, finance
    )
    response = client.get("/discovery/preview")
    assert response.status_code == 200
    body = response.json()
    assert set(body["selected_segments"]) == {"software", "fintech", "finance"}
    assert body["selected_countries"] == ["Finland"]
    assert body["baseline_query_count"] == 3  # 3 segments x 1 country

    # Intersection: backend{software,technology,startup} ∩ ai{ai,software,startup}
    _make_profile(tmp_path, target_countries=["Finland"], role_families=["backend", "ai"])
    body = client.get("/discovery/preview").json()
    assert body["selected_segments"] == ["software", "startup"]


# --- Profile snapshot / discovery timing ------------------------------------


def test_profile_snapshot_validates_and_normalizes_countries(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'snap.db'}"
    init_db(database_url)
    session = get_session()
    try:
        assert profile_snapshot(session).countries == []
        replace_profile(
            session,
            ProfileData(target_countries=["Finland", "sweden", "Nowhere", "FI"]),
        )
        snap = profile_snapshot(session)
        assert snap.countries == ["Finland", "Sweden"]  # deduped, validated
        assert snap.reference is not None
        assert snap.preferences == []
    finally:
        session.close()


def test_profile_parse_run_persists_cv_and_proposes_in_summary(tmp_path, monkeypatch):
    from jobsearch_worker import consumer

    database_url = f"sqlite:///{tmp_path / 'parse.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    init_db(database_url)

    def fake_propose(client, cache_dir, cv_text, description=None):
        return {
            "name": "T", "skills": ["Python"], "job_preferences": [],
            "notes": [], "education": "", "experience": "",
        }

    monkeypatch.setattr("jobsearch_core.run.propose_profile_from_cv", fake_propose)

    session = get_session()
    try:
        run = consumer.enqueue_run(
            session,
            "profile_parse",
            {"cv_text": "My CV text", "description": "backend"},
        )
        consumer.execute_run(session, run)
        session.refresh(run)
        assert run.status == "completed"
        summary = __import__("json").loads(run.summary)
        assert summary["proposal"]["skills"] == ["Python"]
        # Flat wizard-facing mirrors (3.8g/3.8h live-validation fix): the
        # proposal stays nested for the API, and the raw CV text surfaces as
        # extracted_cv_text.
        assert "suggested_role_families" in summary
        assert summary["extracted_cv_text"] == "My CV text"
        assert "never auto-saved" in summary["note"]

        # Raw CV was persisted at parse time; the proposal was NOT.
        row = get_profile_row(session)
        assert row.cv_text == "My CV text"
        assert row.description == "backend"
        assert row.skills == "[]"  # proposal never written to the profile
    finally:
        session.close()


def test_profile_parse_failure_marks_run_failed(tmp_path, monkeypatch):
    from jobsearch_worker import consumer

    database_url = f"sqlite:///{tmp_path / 'parsefail.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    init_db(database_url)

    def failing_propose(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("jobsearch_core.run.propose_profile_from_cv", failing_propose)

    session = get_session()
    try:
        run = consumer.enqueue_run(session, "profile_parse", {"cv_text": "CV"})
        consumer.execute_run(session, run)
        session.refresh(run)
        assert run.status == "failed"
        assert "LLM unavailable" in run.error
        # Raw CV is still persisted even though the parse failed.
        row = get_profile_row(session)
        assert row.cv_text == "CV"
    finally:
        session.close()
