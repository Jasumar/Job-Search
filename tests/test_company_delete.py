"""Bulk company delete (owner request, 2026-11-23).

Properties: a deleted company takes its jobs, recipes, captures, search cache
and run_sources with it; job fingerprints survive in ``job_exclusions``; a
company with an application or a live run is blocked and untouched; a dry run
writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import (
    Application,
    Company,
    CompanyRecipe,
    Job,
    JobExclusion,
    JobRun,
    RunSource,
    SearchCache,
)
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.run import delete_companies


def _company(session, name: str) -> Company:
    company = Company(name=name, careers_url=f"https://{name.lower()}.example/careers")
    session.add(company)
    session.flush()
    return company


def _job(session, company: Company, dedup_hash: str) -> Job:
    job = Job(
        url=f"https://{company.name.lower()}.example/jobs/{dedup_hash}",
        raw_title="Engineer",
        raw_company=company.name,
        normalized_title="engineer",
        normalized_company=company.name.lower(),
        dedup_hash=dedup_hash,
        status="new",
        company_id=company.id,
    )
    session.add(job)
    session.flush()
    return job


@pytest.fixture
def seeded(db_url):
    init_db(db_url)
    session = get_session()
    keep = _company(session, "Keep")
    gone = _company(session, "Gone")
    _job(session, keep, "keep1")
    _job(session, gone, "gone1")
    _job(session, gone, "gone2")
    now = datetime.now(UTC).replace(tzinfo=None)
    session.add(
        CompanyRecipe(
            scope="company_careers", company_id=gone.id, kind="dom", body="{}",
            created_at=now, updated_at=now,
        )
    )
    session.add(SearchCache(company_id=gone.id, query="q", payload="{}", fetched_at=now))
    run = JobRun(run_type="careers_search", status="completed")
    session.add(run)
    session.flush()
    session.add(RunSource(run_id=run.id, company_id=gone.id, status="completed"))
    session.commit()
    ids = {"keep": keep.id, "gone": gone.id}
    session.close()
    return ids


def test_delete_removes_company_jobs_and_references(seeded):
    report = delete_companies([seeded["gone"]])

    assert [c["jobs"] for c in report.deleted] == [2]
    session = get_session()
    assert session.query(Company).count() == 1
    assert [j.dedup_hash for j in session.query(Job).all()] == ["keep1"]
    assert session.query(CompanyRecipe).count() == 0
    assert session.query(SearchCache).count() == 0
    assert session.query(RunSource).count() == 0
    assert {e.dedup_hash for e in session.query(JobExclusion).all()} == {"gone1", "gone2"}
    session.close()


def test_dry_run_writes_nothing(seeded):
    report = delete_companies([seeded["gone"]], dry_run=True)

    assert report.deleted[0]["jobs"] == 2
    session = get_session()
    assert session.query(Company).count() == 2
    assert session.query(Job).count() == 3
    assert session.query(JobExclusion).count() == 0
    session.close()


def test_application_blocks_only_that_company(seeded):
    session = get_session()
    job = session.query(Job).filter(Job.dedup_hash == "gone1").one()
    session.add(Application(job_id=job.id, base_variant="default"))
    session.commit()
    session.close()

    report = delete_companies([seeded["gone"], seeded["keep"]])

    assert [c["name"] for c in report.blocked] == ["Gone"]
    assert [c["name"] for c in report.deleted] == ["Keep"]
    session = get_session()
    assert [c.name for c in session.query(Company).all()] == ["Gone"]
    assert session.query(Job).count() == 2
    session.close()


def test_live_run_blocks_and_unknown_ids_are_reported(seeded):
    session = get_session()
    session.query(RunSource).update({RunSource.status: "running"})
    session.commit()
    session.close()

    report = delete_companies([seeded["gone"], 9999])

    assert report.blocked[0]["reason"] == "a run is still working on it"
    assert report.unknown_ids == [9999]
    assert report.deleted == []


def test_endpoint_dry_run_then_delete(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'cd.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    with TestClient(create_app(database_url)) as client:
        session = get_session()
        company = _company(session, "Gone")
        _job(session, company, "h1")
        session.commit()
        company_id = company.id
        session.close()

        preview = client.post(
            "/companies/delete", json={"company_ids": [company_id], "dry_run": True}
        )
        assert preview.status_code == 200
        assert preview.json()["deleted"][0]["jobs"] == 1

        done = client.post("/companies/delete", json={"company_ids": [company_id]})
        assert done.json()["deleted"][0]["name"] == "Gone"
        assert client.get(f"/companies/{company_id}").status_code == 404
