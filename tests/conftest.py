from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from jobsearch_core.db.session import get_engine, get_session, init_db
from jobsearch_core.profile_evidence import CandidateEvidence


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(scope="session")
def db_template(tmp_path_factory) -> Path:
    """The migrated database every DB test copies.

    Building one costs 1.5 s (create_all + Alembic on a fresh file); a copy is
    microseconds. Sharing it is safe because tests get their own copy, not the
    file itself.
    """
    path = tmp_path_factory.mktemp("db-template") / "template.db"
    init_db(f"sqlite:///{path}")
    engine = get_engine()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    # A copy that misses the write-ahead log would lose the migration rows.
    assert not path.with_name("template.db-wal").exists(), "template WAL not checkpointed"
    return path


@pytest.fixture(autouse=True)
def isolated_db(db_template: Path, tmp_path: Path) -> str:
    """Bind the engine to this test's own empty migrated database.

    Autouse because a test that reads the database without asking for it would
    otherwise see whichever database the previous test left bound — the
    `_known_company_sources()` tests and the discovery tests hit that path.
    """
    target = tmp_path / "test.db"
    shutil.copyfile(db_template, target)
    url = f"sqlite:///{target}"
    init_db(url, ensure_schema=False)
    return url


@pytest.fixture
def db_url(isolated_db: str) -> str:
    return isolated_db


@pytest.fixture
def session(db_url):
    s = get_session()
    yield s
    s.close()
    get_engine().dispose()


@pytest.fixture
def sample_jobs():
    return [
        {
            "url": "https://example.com/job/1",
            "raw_title": "Junior Python Developer",
            "raw_company": "Example Corp",
            "raw_location": "Helsinki, Finland",
            "description": "Looking for a Python developer with 1-2 years of experience.",
        },
        {
            "url": "https://example.com/job/2",
            "raw_title": "Senior Staff Software Engineer",
            "raw_company": "BigTech Inc",
            "raw_location": "Berlin, Germany",
            "description": "10+ years of experience required. Lead teams of 20+ engineers.",
        },
        {
            "url": "https://example.com/job/3",
            "raw_title": "Frontend Developer",
            "raw_company": "WebApp Ltd",
            "raw_location": "Stockholm, Sweden",
            "description": "React and Angular frontend developer with 3 years experience.",
        },
    ]


@pytest.fixture
def evidence() -> CandidateEvidence:
    """A candidate evidence projection for scoring/tailoring tests.

    Phase 4.1a replaced the ``Evidence`` YAML model; the projection is now
    assembled from the two owner tables, so tests build the flat form.
    """
    return CandidateEvidence(
        name="Test User",
        location="Helsinki, Finland",
        citizenship="Finnish",
        education=[
            {"degree": "BSc", "field": "Computer Science", "institution": "University of Helsinki", "year": 2022}
        ],
        experience=[
            {
                "title": "Python Developer",
                "company": "CurrentCo",
                "start": "2022-06",
                "end": "present",
                "responsibilities": ["Built internal tools"],
                "technologies": ["Python", "SQL", "Django"],
            }
        ],
        skills=["Python", "SQL", "Django", "Docker", "Git"],
        languages=[
            {"language": "Finnish", "proficiency": "native"},
            {"language": "English", "proficiency": "fluent"},
        ],
        target_roles=["backend", "python", "data"],
        target_countries=["Finland", "Sweden", "Estonia", "Germany", "Netherlands"],
        remote_only=False,
        visa_required=False,
    )


@pytest.fixture
def evidence_dict(evidence):
    """Legacy-shaped dict, for tests that assert on serialized payloads."""
    return evidence.model_dump()
