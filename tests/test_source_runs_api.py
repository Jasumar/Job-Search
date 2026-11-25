"""Lane B1: the M1 source-run API surface.

Covers the contract M1-UI builds against — enqueue of ``careers_search`` with
its per-company manifest, run pause/resume, ``GET /runs/{id}/sources`` and the
no-careers company selection list — plus the worker-side rules behind them
(paused runs are not claimed and are never reported completed).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Company, JobRun
from jobsearch_core.db.session import get_session
from jobsearch_worker.consumer import (
    claim_next_run,
    enqueue_run,
    execute_run,
    request_pause,
    request_resume,
)


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


def _company(
    name: str,
    *,
    careers_url: str | None = None,
    country: str = "FI",
    status: str = "pending",
    entry: str | None = None,
) -> Company:
    return Company(
        name=name,
        status=status,
        country=country,
        careers_url=careers_url,
        open_positions_url=entry,
        open_positions_landing_url=f"{entry}/landing" if entry else None,
    )


def _seed(*companies: Company) -> list[int]:
    session = get_session()
    try:
        session.add_all(companies)
        session.commit()
        return [company.id for company in companies]
    finally:
        session.close()


def test_careers_search_enqueues_run_and_manifest(api_client):
    first, second, third = _seed(
        _company("Alpha Oy"),
        _company("Beta Oy"),
        _company("Gamma Oy"),
    )

    # The selection is deduplicated but keeps the owner's order.
    response = api_client.post(
        "/runs/careers-search",
        json={"company_ids": [third, first, third], "batch_size": 10, "page_ceiling": 3},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    run_id = response.json()["run_id"]

    run = api_client.get(f"/runs/{run_id}").json()
    assert run["run_type"] == "careers_search"
    assert run["params"] == {
        "company_ids": [third, first],
        "batch_size": 10,
        "page_ceiling": 3,
    }

    sources = api_client.get(f"/runs/{run_id}/sources").json()
    assert [row["company_id"] for row in sources] == [third, first]
    assert [row["company_name"] for row in sources] == ["Gamma Oy", "Alpha Oy"]
    assert {row["status"] for row in sources} == {"queued"}
    assert all(row["source_id"] is None and row["source_url"] is None for row in sources)
    assert all(row["candidates_added"] == 0 for row in sources)


def test_careers_search_rejects_bad_selection_without_creating_a_run(api_client):
    known, paged = _seed(
        _company("Known Oy"),
        _company("HasCareers Oy", careers_url="https://hascareers.example/jobs"),
    )

    response = api_client.post("/runs/careers-search", json={"company_ids": []})
    assert response.status_code == 422

    response = api_client.post("/runs/careers-search", json={"company_ids": [known, 999]})
    assert response.status_code == 422
    assert "999" in response.json()["detail"]

    response = api_client.post("/runs/careers-search", json={"company_ids": [paged]})
    assert response.status_code == 422
    assert str(paged) in response.json()["detail"]

    assert api_client.get("/runs").json() == []


def test_careers_search_allows_one_active_run_and_paused_holds_the_slot(api_client):
    (company_id,) = _seed(_company("Only Oy"))

    first = api_client.post("/runs/careers-search", json={"company_ids": [company_id]})
    assert first.status_code == 202
    run_id = first.json()["run_id"]

    assert (
        api_client.post("/runs/careers-search", json={"company_ids": [company_id]}).status_code
        == 409
    )

    paused = api_client.post(f"/runs/{run_id}/pause")
    assert paused.status_code == 200
    assert paused.json() == {"id": run_id, "status": "paused"}

    # A paused run still holds the run-type slot.
    assert (
        api_client.post("/runs/careers-search", json={"company_ids": [company_id]}).status_code
        == 409
    )

    resumed = api_client.post(f"/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json() == {"id": run_id, "status": "queued"}
    assert api_client.get(f"/runs/{run_id}").json()["started_at"] is None

    # A second run is still refused: the first one is queued again.
    assert (
        api_client.post("/runs/careers-search", json={"company_ids": [company_id]}).status_code
        == 409
    )


def test_pause_resume_transitions(api_client):
    assert api_client.post("/runs/999/pause").status_code == 404
    assert api_client.post("/runs/999/resume").status_code == 404

    (company_id,) = _seed(_company("Transition Oy"))
    run_id = api_client.post(
        "/runs/careers-search", json={"company_ids": [company_id]}
    ).json()["run_id"]

    # Resume of a non-paused run is a conflict; pause is idempotent.
    assert api_client.post(f"/runs/{run_id}/resume").status_code == 409
    assert api_client.post(f"/runs/{run_id}/pause").json()["status"] == "paused"
    assert api_client.post(f"/runs/{run_id}/pause").json()["status"] == "paused"

    session = get_session()
    try:
        run = session.get(JobRun, run_id)
        run.status = "completed"
        session.commit()
    finally:
        session.close()

    assert api_client.post(f"/runs/{run_id}/pause").status_code == 409
    assert api_client.post(f"/runs/{run_id}/resume").status_code == 409


def test_run_sources_404_and_empty_for_other_run_types(api_client):
    assert api_client.get("/runs/999/sources").status_code == 404

    session = get_session()
    try:
        run = enqueue_run(session, "discover", {"dry_run": True})
        run_id = run.id
    finally:
        session.close()

    response = api_client.get(f"/runs/{run_id}/sources")
    assert response.status_code == 200
    assert response.json() == []


def test_companies_no_careers_selection_list(api_client):
    missing, with_page, blank, other_country = _seed(
        _company("Missing Oy"),
        _company("WithPage Oy", careers_url="https://withpage.example/careers"),
        _company("Blank Oy", careers_url=""),
        _company("Swedish Oy", country="SE"),
    )

    response = api_client.get("/companies", params={"no_careers": 1, "country": "fi"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [missing, blank]

    # Without the flag the full list (both countries) is unchanged.
    assert len(api_client.get("/companies").json()) == 4
    assert with_page not in [row["id"] for row in api_client.get("/companies", params={"no_careers": 1}).json()]
    assert other_country in [row["id"] for row in api_client.get("/companies", params={"no_careers": 1}).json()]


def test_no_careers_list_excludes_a_row_accepted_over_its_entry(api_client):
    """The selection list asks "who still needs a source", not "who lacks a URL".

    An accepted row whose entry is resolved is scrapable, so offering it again
    would spend a search on a company the owner already decided (and the worker
    would refuse it anyway).
    """
    accepted_entry, accepted_no_entry, pending_entry, known_entry = _seed(
        _company("Accepted Entry Oy", status="accepted", entry="https://a.example/jobs"),
        _company("Accepted No Entry Oy", status="accepted"),
        _company("Pending Entry Oy", status="pending", entry="https://b.example/jobs"),
        _company("Known Entry Oy", status="known", entry="https://c.example/jobs"),
    )

    listed = [
        row["id"] for row in api_client.get("/companies", params={"no_careers": 1}).json()
    ]
    assert accepted_entry not in listed
    assert known_entry not in listed
    # No entry means nothing to scrape: still offered.
    assert accepted_no_entry in listed
    # pending is the correction path — a wrong entry stays re-resolvable.
    assert pending_entry in listed

    # The enqueue refuses the same rows the list no longer offers.
    response = api_client.post("/runs/careers-search", json={"company_ids": [accepted_entry]})
    assert response.status_code == 422
    assert "hiring source" in response.json()["detail"]

    allowed = api_client.post("/runs/careers-search", json={"company_ids": [pending_entry]})
    assert allowed.status_code == 202


def test_worker_does_not_claim_a_paused_run(api_client):
    (company_id,) = _seed(_company("Claim Oy"))
    run_id = api_client.post(
        "/runs/careers-search", json={"company_ids": [company_id]}
    ).json()["run_id"]
    api_client.post(f"/runs/{run_id}/pause")

    session = get_session()
    try:
        assert claim_next_run(session) is None
        assert request_pause(session, run_id) == "paused"
        assert request_resume(session, run_id) == "queued"
        claimed = claim_next_run(session)
        assert claimed is not None and claimed.id == run_id
    finally:
        session.close()


def test_execute_run_does_no_work_when_paused_before_start(api_client, monkeypatch):
    (company_id,) = _seed(_company("Skip Oy"))
    run_id = api_client.post(
        "/runs/careers-search", json={"company_ids": [company_id]}
    ).json()["run_id"]

    called = []
    monkeypatch.setattr(
        "jobsearch_worker.consumer.run_workflow",
        lambda **kwargs: called.append(kwargs) or {},
    )

    session = get_session()
    try:
        api_client.post(f"/runs/{run_id}/pause")
        run = session.get(JobRun, run_id)
        execute_run(session, run)
        session.refresh(run)
        assert called == []
        assert run.status == "paused"
        assert run.finished_at is None and run.summary is None
    finally:
        session.close()


def test_pause_during_work_is_not_overwritten_with_completed(api_client, monkeypatch):
    session = get_session()
    try:
        run = enqueue_run(session, "profile_parse", {"cv_text": "cv body"})
        run_id = run.id
    finally:
        session.close()

    def pause_mid_work(**_kwargs):
        inner = get_session()
        try:
            inner.get(JobRun, run_id).status = "paused"
            inner.commit()
        finally:
            inner.close()
        return {"parsed": True}

    monkeypatch.setattr("jobsearch_worker.consumer.parse_profile_workflow", pause_mid_work)

    session = get_session()
    try:
        claimed = claim_next_run(session)
        assert claimed is not None and claimed.id == run_id
        execute_run(session, claimed)
        session.refresh(claimed)
        assert claimed.status == "paused"
        assert claimed.finished_at is None
        assert claimed.summary is None
    finally:
        session.close()
