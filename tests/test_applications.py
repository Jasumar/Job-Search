from __future__ import annotations

import pytest
from click.testing import CliRunner
from jobsearch_core.cli import jobs
from jobsearch_core.db.models import Application, Base, CVRevision, Job
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", echo=False)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    s: Session = session_factory()
    yield s
    s.close()
    engine.dispose()


class TestApplicationModel:
    def test_create_application(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/1",
            raw_title="Python Developer",
            raw_company="Test Corp",
            raw_location="Helsinki",
            description="Python job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(
            job_id=job.id,
            base_variant="backend",
            status="approved",
        )
        session.add(app)
        session.commit()

        fetched = session.query(Application).first()
        assert fetched is not None
        assert fetched.job_id == job.id
        assert fetched.base_variant == "backend"
        assert fetched.status == "approved"

    def test_application_job_relationship(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/2",
            raw_title="Data Engineer",
            raw_company="Data Corp",
            raw_location="Turku",
            description="Data job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(
            job_id=job.id,
            base_variant="data",
            status="draft",
        )
        session.add(app)
        session.commit()

        assert app.job.raw_title == "Data Engineer"
        assert job.applications[0].base_variant == "data"

    def test_application_status_default(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/3",
            raw_title="ML Engineer",
            raw_company="AI Corp",
            raw_location="Helsinki",
            description="ML job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="ai")
        session.add(app)
        session.commit()

        assert app.status == "draft"

    def test_application_with_tailored_path(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/4",
            raw_title="Backend Dev",
            raw_company="Back Corp",
            raw_location="Helsinki",
            description="Backend job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(
            job_id=job.id,
            base_variant="backend",
            status="approved",
            tailored_typst_path="data/applications/job_4/cv.typst",
        )
        session.add(app)
        session.commit()

        fetched = session.query(Application).first()
        assert fetched.tailored_typst_path == "data/applications/job_4/cv.typst"

    def test_application_timestamps(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/5",
            raw_title="Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend")
        session.add(app)
        session.commit()

        assert app.created_at is not None
        assert app.updated_at is not None


class TestCVRevisionModel:
    def test_create_revision(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/6",
            raw_title="Python Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Python",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend", status="approved")
        session.add(app)
        session.flush()

        rev = CVRevision(
            application_id=app.id,
            diff_text="--- a/original\n+++ b/tailored\n@@ -1 +1 @@\n-summary\n+new summary",
        )
        session.add(rev)
        session.commit()

        fetched = session.query(CVRevision).first()
        assert fetched is not None
        assert fetched.application_id == app.id
        assert "new summary" in fetched.diff_text

    def test_revision_application_relationship(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/7",
            raw_title="Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend", status="approved")
        session.add(app)
        session.flush()

        rev = CVRevision(application_id=app.id)
        session.add(rev)
        session.commit()

        assert rev.application.id == app.id
        assert app.revisions[0].id == rev.id

    def test_revision_timestamps(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/8",
            raw_title="Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend")
        session.add(app)
        session.flush()

        rev = CVRevision(application_id=app.id)
        session.add(rev)
        session.commit()

        assert rev.created_at is not None

    def test_cascade_delete_application(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/9",
            raw_title="Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend", status="approved")
        session.add(app)
        session.flush()

        session.add(CVRevision(application_id=app.id))
        session.add(CVRevision(application_id=app.id))
        session.commit()

        assert session.query(CVRevision).count() == 2
        session.delete(app)
        session.commit()
        assert session.query(CVRevision).count() == 0

    def test_cascade_delete_job(self, db_session):
        session = db_session
        job = Job(
            url="https://example.com/job/10",
            raw_title="Dev",
            raw_company="Co",
            raw_location="Helsinki",
            description="Job",
            status="new",
        )
        session.add(job)
        session.flush()

        app = Application(job_id=job.id, base_variant="backend", status="approved")
        session.add(app)
        session.flush()

        session.add(CVRevision(application_id=app.id))
        session.commit()

        assert session.query(Application).count() == 1
        session.delete(job)
        session.commit()
        assert session.query(Application).count() == 0
        assert session.query(CVRevision).count() == 0


class TestJobsList:
    def _setup_db(self, tmp_path, seed):
        db_url = f"sqlite:///{tmp_path}/jobs.db"
        from jobsearch_core.db.session import get_engine, get_session, init_db

        init_db(db_url)
        s = get_session()
        seed(s)
        s.commit()
        s.close()
        return {"url": db_url, "engine": get_engine()}

    def _run(self, monkeypatch, db_url, *args):
        # Phase 4.2a: the jobs list query lives in run.py list_jobs, which
        # resolves Settings from its OWN module namespace — patching
        # jobsearch_core.cli.Settings alone does not cover it (same rule as
        # the collect workflow patch targets in test_scrape_pipeline.py).
        class FakeSettings:
            def __init__(self):
                self.database_url = db_url

        monkeypatch.setattr("jobsearch_core.run.Settings", FakeSettings)
        monkeypatch.setattr("jobsearch_core.cli.Settings", FakeSettings)
        from jobsearch_core.db.session import get_session, init_db

        init_db(db_url)
        runner = CliRunner()
        result = runner.invoke(jobs, ["list", *args])
        s = get_session()
        s.close()
        return result

    def test_lists_all_jobs_with_header(self, monkeypatch, tmp_path):
        def seed(s):
            s.add(Job(url="https://example.com/job/1", raw_title="Python Dev", raw_company="ACME",
                      country="fi", status="new", total_score=0.8, posted_date="2026-08-01"))

        info = self._setup_db(tmp_path, seed)
        result = self._run(monkeypatch, info["url"])
        assert result.exit_code == 0
        assert "Python Dev @ ACME" in result.output

    def test_status_and_country_filters(self, monkeypatch, tmp_path):
        def seed(s):
            s.add(Job(url="https://e.com/1", raw_title="One", raw_company="A", country="fi", status="new"))
            s.add(Job(url="https://e.com/2", raw_title="Two", raw_company="B", country="de", status="rejected"))

        info = self._setup_db(tmp_path, seed)
        out = self._run(monkeypatch, info["url"], "--status", "new", "--country", "fi").output
        assert "One @ A" in out
        assert "Two @ B" not in out

    def test_min_score_and_scored_filters(self, monkeypatch, tmp_path):
        def seed(s):
            s.add(Job(url="https://e.com/1", raw_title="High", raw_company="A", country="fi",
                      status="new", total_score=0.9))
            s.add(Job(url="https://e.com/2", raw_title="Low", raw_company="B", country="fi",
                      status="new", total_score=0.3))
            s.add(Job(url="https://e.com/3", raw_title="None", raw_company="C", country="fi",
                      status="new"))

        info = self._setup_db(tmp_path, seed)
        out = self._run(monkeypatch, info["url"], "--min-score", "0.5").output
        assert "High @ A" in out
        assert "Low @ B" not in out
        assert "None @ C" not in out

        out2 = self._run(monkeypatch, info["url"], "--unscored").output
        assert "None @ C" in out2
        assert "High @ A" not in out2

    def test_tailored_filter(self, monkeypatch, tmp_path):
        def seed(s):
            job = Job(url="https://e.com/1", raw_title="Tailored", raw_company="A", country="fi", status="new")
            s.add(job)
            s.flush()
            s.add(Application(job_id=job.id, base_variant="backend", status="approved"))
            s.add(Job(url="https://e.com/2", raw_title="Untailored", raw_company="B", country="fi", status="new"))

        info = self._setup_db(tmp_path, seed)
        out = self._run(monkeypatch, info["url"], "--tailored").output
        assert "Tailored @ A" in out
        assert "Untailored @ B" not in out

        out2 = self._run(monkeypatch, info["url"], "--not-tailored").output
        assert "Untailored @ B" in out2
        assert "Tailored @ A" not in out2
