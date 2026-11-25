"""Manual delete of a posting that is not a job (owner request).

Property: the row is gone, the fingerprint survives in ``job_exclusions``, and
the next scrape skips the same posting instead of re-adding it. Deleting an id
that no longer exists reports "not found" rather than a silent success.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Job, JobExclusion
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pipeline import ingest_scraped
from jobsearch_core.run import delete_job


def _job(dedup_hash: str = "abc123") -> Job:
    return Job(
        url="https://example.com/news/press-release",
        raw_title="Press releaseExample Corp wins an award",
        raw_company="Example Corp",
        raw_location="Helsinki, Finland",
        normalized_title="press releaseexample corp wins an award",
        normalized_company="example corp",
        dedup_hash=dedup_hash,
        status="new",
    )


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'delete.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


def test_delete_removes_row_and_keeps_fingerprint(db_url):
    init_db(db_url)
    session = get_session()
    session.add(_job())
    session.commit()
    job_id = session.query(Job).one().id
    session.close()

    assert delete_job(job_id) is True

    session = get_session()
    assert session.query(Job).count() == 0
    exclusion = session.query(JobExclusion).one()
    assert exclusion.dedup_hash == "abc123"
    assert exclusion.url == "https://example.com/news/press-release"
    assert exclusion.raw_title == "Press releaseExample Corp wins an award"
    session.close()


def test_delete_unknown_id_reports_not_found(db_url):
    init_db(db_url)
    assert delete_job(424242) is False


def test_ingest_skips_a_posting_a_human_deleted(db_url):
    init_db(db_url)
    scraped = [
        ScrapedJob(
            url="https://example.com/news/press-release",
            title="Backend Engineer",
            company="Example Corp",
            location="Helsinki, Finland",
            description="We need a Python developer with Django experience.",
        )
    ]
    first = ingest_scraped(scraped, source_url="https://example.com/careers")
    assert first["added"] == 1

    session = get_session()
    job_id = session.query(Job).one().id
    session.close()
    assert delete_job(job_id) is True

    second = ingest_scraped(scraped, source_url="https://example.com/careers")
    assert second["excluded"] == 1
    assert second["added"] == 0

    session = get_session()
    assert session.query(Job).count() == 0
    session.close()


def test_delete_endpoint_returns_204_then_404(api_client):
    session = get_session()
    session.add(_job())
    session.commit()
    job_id = session.query(Job).one().id
    session.close()

    response = api_client.delete(f"/jobs/{job_id}")
    assert response.status_code == 204

    session = get_session()
    assert session.query(Job).count() == 0
    assert session.query(JobExclusion).count() == 1
    session.close()

    # A second click reports the truth instead of pretending to delete again.
    assert api_client.delete(f"/jobs/{job_id}").status_code == 404
