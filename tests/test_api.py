from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Application, Company, Job
from jobsearch_core.db.session import get_session


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


def _company(name: str, *, status: str = "pending", country: str = "FI") -> Company:
    return Company(
        name=name,
        status=status,
        country=country,
        careers_url=f"https://{name.lower()}.example/careers",
    )


def test_companies_list_filter_add_and_404(api_client):
    session = get_session()
    session.add_all([_company("Pending", status="pending"), _company("Accepted", status="accepted")])
    session.commit()
    session.close()

    response = api_client.get("/companies", params={"status": "pending", "country": "fi"})
    assert response.status_code == 200
    assert [company["name"] for company in response.json()] == ["Pending"]

    response = api_client.get("/companies/999")
    assert response.status_code == 404

    response = api_client.post(
        "/companies",
        json={
            "name": "Manual",
            "careers_url": "https://manual.example/jobs",
            "country": "fi",
            "notes": "trusted source",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "known"
    assert response.json()["verification_status"] == "verified"


def test_review_accept_replaces_url_and_verifies(api_client):
    session = get_session()
    company = _company("Review Me")
    session.add(company)
    session.commit()
    company_id = company.id
    session.close()

    response = api_client.post(
        f"/companies/{company_id}/review",
        json={
            "status": "accepted",
            "careers_url": "https://review.example/new-careers",
            "open_positions_url": "https://review.example/new-openings",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1}

    response = api_client.get(f"/companies/{company_id}")
    assert response.json()["verification_status"] == "verified"
    assert response.json()["careers_url"] == "https://review.example/new-careers"
    assert response.json()["open_positions_url"] == "https://review.example/new-openings"


def test_review_accept_with_one_url_fills_the_other(api_client):
    session = get_session()
    company = _company("One URL")
    session.add(company)
    session.commit()
    company_id = company.id
    session.close()

    response = api_client.post(
        f"/companies/{company_id}/review",
        json={"status": "accepted", "open_positions_url": "https://one.example/openings"},
    )
    assert response.status_code == 200

    response = api_client.get(f"/companies/{company_id}")
    assert response.json()["careers_url"] == "https://one.example/openings"
    assert response.json()["open_positions_url"] == "https://one.example/openings"


def test_review_rejects_invalid_status_and_unknown_company(api_client):
    assert api_client.post("/companies/1/review", json={"status": "approved"}).status_code == 422
    assert api_client.post("/companies/999/review", json={"status": "rejected"}).status_code == 404


def test_jobs_filters_min_score_scored_and_tailored(api_client):
    session = get_session()
    scored = Job(
        url="https://example.com/scored",
        raw_title="Scored job",
        raw_company="Example",
        raw_location="Helsinki",
        country="FI",
        status="new",
        total_score=0.8,
    )
    unscored = Job(
        url="https://example.com/unscored",
        raw_title="Unscored job",
        raw_company="Example",
        raw_location="Helsinki",
        country="FI",
        status="new",
    )
    session.add_all([scored, unscored])
    session.commit()
    session.add(Application(job_id=scored.id, base_variant="backend"))
    session.commit()
    session.close()

    assert [job["url"] for job in api_client.get("/jobs", params={"min_score": 0.7}).json()] == [
        "https://example.com/scored"
    ]
    assert [job["url"] for job in api_client.get("/jobs", params={"scored": "false"}).json()] == [
        "https://example.com/unscored"
    ]
    tailored = api_client.get("/jobs", params={"tailored": "true"}).json()
    assert [job["url"] for job in tailored] == ["https://example.com/scored"]
    assert tailored[0]["has_applications"] is True


def test_discovery_defaults_to_pending_queue(api_client):
    session = get_session()
    session.add_all([_company("Pending"), _company("Rejected", status="rejected")])
    session.commit()
    session.close()

    response = api_client.get("/discovery")
    assert response.status_code == 200
    assert [company["name"] for company in response.json()] == ["Pending"]
