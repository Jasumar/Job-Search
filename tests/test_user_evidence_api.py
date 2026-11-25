"""Phase 4.1a API surface: contact fields and the evidence/additional-experience endpoints.

The endpoint shape encodes invariant (c): there is a GET and a single
targeted PUT, and no full-replace evidence route, so an LLM proposal cannot be
persisted by anything but a deliberate user action.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


CONTACT = {
    "location": "Tampere, Finland",
    "citizenship": "Finnish",
    "linkedin": "https://linkedin.com/in/t",
    "gitlab": "https://gitlab.com/t",
    "email": "t@example.com",
    "phone": "+358401234567",
    "visa_required": True,
}


def test_profile_roundtrips_contact_fields(api_client):
    body = {"name": "Test User", "skills": ["Python"], **CONTACT}
    put = api_client.put("/profile", json=body)
    assert put.status_code == 200, put.text

    got = api_client.get("/profile").json()["profile"]
    for key, value in CONTACT.items():
        assert got[key] == value
    assert got["location"] == "Tampere, Finland"


def test_evidence_endpoint_is_empty_before_anything_is_written(api_client):
    resp = api_client.get("/profile/evidence")
    assert resp.status_code == 200
    assert resp.json()["skills"] == []
    assert resp.json()["additional_experience"] == ""


def test_additional_experience_roundtrips_verbatim(api_client):
    text = "  Freelance work in 2019.\n\nKept   spacing and newlines.  "
    resp = api_client.put(
        "/profile/additional-experience", json={"additional_experience": text}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["additional_experience"] == text
    assert api_client.get("/profile/evidence").json()["additional_experience"] == text


def test_put_evidence_replaces_facts_and_keeps_additional_experience(api_client):
    """MF1: the owner's Save writes the confirmed proposal in one replace.

    ``additional_experience`` has its own writer and is never touched.
    """
    api_client.put("/profile/additional-experience", json={"additional_experience": "  mine "})
    body = {
        "skills": ["Python", " ", "SQL"],
        "experience": [
            {"title": "Developer", "company": "Acme", "start": "2019", "end": "present",
             "technologies": ["Python"]}
        ],
        "languages": [{"language": "Finnish", "proficiency": "native"}],
    }
    resp = api_client.put("/profile/evidence", json=body)
    assert resp.status_code == 200, resp.text
    stored = api_client.get("/profile/evidence").json()
    assert stored["skills"] == ["Python", "SQL"]
    assert stored["experience"][0]["title"] == "Developer"
    assert stored["languages"] == [{"language": "Finnish", "proficiency": "native"}]
    assert stored["additional_experience"] == "  mine "

    # Full replace: what is not sent is gone.
    api_client.put("/profile/evidence", json={"skills": ["Go"]})
    stored = api_client.get("/profile/evidence").json()
    assert stored["skills"] == ["Go"]
    assert stored["experience"] == []


def test_put_evidence_refuses_an_experience_entry_without_title(api_client):
    resp = api_client.put("/profile/evidence", json={"experience": [{"company": "Acme"}]})
    assert resp.status_code == 422


def test_cv_submission_carries_additional_experience_to_the_run(api_client):
    """The value must reach the run params, not just the response schema."""
    text = "Freelance work in 2019"
    resp = api_client.post(
        "/profile/cv",
        json={"cv_text": "Experienced Python developer...", "additional_experience": text},
    )
    assert resp.status_code == 202, resp.text
    run = api_client.get(f"/runs/{resp.json()['run_id']}").json()
    assert run["params"]["additional_experience"] == text
