from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from jobsearch_api import routes
from jobsearch_api.app import create_app
from jobsearch_core.config import PipelineDefaults
from jobsearch_core.db.session import get_session
from jobsearch_core.run import RunSummary
from jobsearch_core.scoring.match import CONSIDER, SKIP, WORTH
from jobsearch_worker import consumer


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'runs.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url, start_worker=False)
    with TestClient(app) as client:
        # MF1: a match run needs saved skills or experience to start.
        assert client.put("/profile/evidence", json={"skills": ["Python"]}).status_code == 200
        yield client


def test_match_run_is_refused_without_skills_or_experience(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url, start_worker=False)
    with TestClient(app) as client:
        response = client.post("/runs/match", json={})
        assert response.status_code == 422
        assert "no skills and no experience" in response.json()["detail"]
        assert client.get("/runs").json() == []
        assert client.get("/matches/status").json()["evidence_ready"] is False


def _wait_for(client: TestClient, run_id: int, status: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        if response.json()["status"] == status:
            return response.json()
        time.sleep(0.02)
    pytest.fail(f"run {run_id} did not reach {status}")


def test_enqueue_list_get_and_unknown(api_client):
    response = api_client.post("/runs/discover", json={"dry_run": True, "max_companies": 999})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert api_client.get(f"/runs/{run_id}").json()["params"]["dry_run"] is True
    assert api_client.get(f"/runs/{run_id}").json()["status"] == "queued"
    assert api_client.get("/runs/999").status_code == 404

    response = api_client.post("/runs/collect", json={"dry_run": False})
    assert response.status_code == 202
    assert {run["run_type"] for run in api_client.get("/runs").json()} == {"discover", "collect"}


def test_runs_status_filter_combines_with_limit(api_client):
    """Regression: filter() must be applied before limit(), and limit must still cap results.

    Both halves matter. Filtering first keeps SQLAlchemy from raising
    InvalidRequestError when .filter() is chained onto a query that already has
    LIMIT applied; applying limit last keeps the pagination contract intact.
    """
    # Only one run per type may be active, so vary the type to get multiple rows.
    assert api_client.post("/runs/discover", json={"dry_run": True}).status_code == 202
    assert api_client.post("/runs/collect", json={"dry_run": True}).status_code == 202
    assert api_client.post("/runs/match", json={}).status_code == 202

    assert len(api_client.get("/runs").json()) == 3

    # A status filter must no longer 500.
    queued = api_client.get("/runs?status=queued")
    assert queued.status_code == 200, queued.text
    assert len(queued.json()) == 3
    assert all(run["status"] == "queued" for run in queued.json())

    # A status nothing matches returns an empty list, not an error.
    assert api_client.get("/runs?status=failed").json() == []

    # limit is applied after the filter: 3 queued runs exist, limit=2 returns 2.
    limited = api_client.get("/runs?status=queued&limit=2").json()
    assert len(limited) == 2
    assert all(run["status"] == "queued" for run in limited)

    # Newest-first ordering survives the filter (match was queued last).
    assert api_client.get("/runs?status=queued").json()[0]["run_type"] == "match"


def test_active_run_is_per_type_and_dry_run_is_required(api_client):
    assert api_client.post("/runs/discover", json={}).status_code == 422
    assert api_client.post("/runs/collect", json={}).status_code == 422
    assert api_client.post("/runs/discover", json={"dry_run": True}).status_code == 202
    assert api_client.post("/runs/discover", json={"dry_run": False}).status_code == 409
    assert api_client.post("/runs/collect", json={"dry_run": True}).status_code == 202


def test_caps_are_clamped(api_client, monkeypatch):
    monkeypatch.setattr(routes, "load_defaults", lambda: PipelineDefaults(max_companies=3))
    response = api_client.post("/runs/discover", json={"dry_run": True, "max_companies": 9})
    assert response.status_code == 202
    run = api_client.get(f"/runs/{response.json()['run_id']}").json()
    assert run["params"]["max_companies"] == 3


def test_match_run_is_queued_with_verbatim_metadata(api_client):
    response = api_client.post(
        "/runs/match",
        json={"job_types": ["backend", "ai"], "cv_variant": "backend"},
    )
    assert response.status_code == 202
    run = api_client.get(f"/runs/{response.json()['run_id']}").json()
    assert run["run_type"] == "match"
    assert run["status"] == "queued"
    # Traceability metadata is recorded, never silently dropped.
    assert run["params"]["job_types"] == ["backend", "ai"]
    assert run["params"]["cv_variant"] == "backend"
    assert run["params"]["llm_notes"] is False
    # A match run needs no dry-run gate: matching is deterministic by default.
    assert "dry_run" not in run["params"]


def test_active_match_run_blocks_a_second_match(api_client):
    assert api_client.post("/runs/match", json={}).status_code == 202
    assert api_client.post("/runs/match", json={}).status_code == 409


def test_worker_completes_match_run(api_client, monkeypatch):
    seen = {}

    def fake_match(**kwargs):
        seen.update(kwargs)
        return RunSummary(
            dry_run=False,
            mode="match",
            jobs_scored=3,
            recommendations={WORTH: ["a", "b"], CONSIDER: ["c"], SKIP: []},
        )

    monkeypatch.setattr(consumer, "match_jobs", fake_match)
    run_id = api_client.post("/runs/match", json={"llm_notes": True}).json()["run_id"]
    worker = consumer.RunWorker()
    worker.start()
    try:
        result = _wait_for(api_client, run_id, "completed")
    finally:
        worker.stop()
    assert result["summary"]["mode"] == "match"
    assert result["summary"]["jobs_scored"] == 3
    assert len(result["summary"]["recommendations"][WORTH]) == 2
    # The worker forwards recorded params to the matcher; metadata is not a filter.
    assert seen["llm_notes"] is True


def test_worker_fails_match_run_with_traceable_error(api_client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("match exploded")

    monkeypatch.setattr(consumer, "match_jobs", boom)
    run_id = api_client.post("/runs/match", json={}).json()["run_id"]
    worker = consumer.RunWorker()
    worker.start()
    try:
        result = _wait_for(api_client, run_id, "failed")
    finally:
        worker.stop()
    assert "match exploded" in result["error"]


def test_queued_match_cancel_is_abandoned_without_workflow(api_client, monkeypatch):
    called = False

    def fail_match(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("cancelled match run was executed")

    monkeypatch.setattr(consumer, "match_jobs", fail_match)
    run_id = api_client.post("/runs/match", json={}).json()["run_id"]
    assert api_client.post(f"/runs/{run_id}/cancel").json()["status"] == "cancelled_requested"
    worker = consumer.RunWorker()
    worker.start()
    try:
        _wait_for(api_client, run_id, "cancelled")
    finally:
        worker.stop()
    assert called is False


def test_worker_completes_run(api_client, monkeypatch):
    def fake_workflow(**kwargs):
        return RunSummary(dry_run=kwargs["dry_run"], mode="discover", jobs_discovered=2)

    monkeypatch.setattr(consumer, "run_workflow", fake_workflow)
    response = api_client.post("/runs/discover", json={"dry_run": True})
    run_id = response.json()["run_id"]
    worker = consumer.RunWorker()
    worker.start()
    try:
        result = _wait_for(api_client, run_id, "completed")
    finally:
        worker.stop()
    assert result["summary"]["jobs_discovered"] == 2


def test_queued_cancel_is_abandoned_without_workflow(api_client, monkeypatch):
    called = False

    def fail_workflow(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("cancelled run was executed")

    monkeypatch.setattr(consumer, "run_workflow", fail_workflow)
    run_id = api_client.post("/runs/discover", json={"dry_run": True}).json()["run_id"]
    assert api_client.post(f"/runs/{run_id}/cancel").json()["status"] == "cancelled_requested"
    worker = consumer.RunWorker()
    worker.start()
    try:
        _wait_for(api_client, run_id, "cancelled")
    finally:
        worker.stop()
    assert called is False


def test_claim_next_run_claims_once(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'claim.db'}"
    from jobsearch_core.db.session import init_db

    init_db(database_url)
    session = get_session()
    try:
        run = consumer.enqueue_run(session, "discover", {"dry_run": True})
        first = consumer.claim_next_run(session)
        second = consumer.claim_next_run(session)
        assert first is not None and first.id == run.id
        assert second is None
    finally:
        session.close()


def test_consumer_has_no_fastapi_import():
    from pathlib import Path

    source = Path(consumer.__file__).read_text(encoding="utf-8")
    assert "import fastapi" not in source.lower()
