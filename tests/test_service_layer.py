"""Phase 4.2a: service functions are callable from a non-Click context.

run.py must expose run_workflow / collect_workflow / match_jobs /
list_jobs without requiring Click: no echo callback means progress goes to the
logger, and no code path raises click's SystemExit/Abort.
"""

from unittest.mock import patch

import pytest
from jobsearch_core.db.models import Job
from jobsearch_core.run import RunSummary, list_jobs


def _add_job(db_url, **kwargs):
    from jobsearch_core.db.session import get_session, init_db

    init_db(db_url)
    session = get_session()
    job = Job(
        url=kwargs.get("url", f"https://example.com/job/{next(_add_job._counter)}"),
        raw_title=kwargs.get("raw_title", "Backend Developer"),
        raw_company=kwargs.get("raw_company", "Example Oy"),
        description=kwargs.get("description", "Python and PostgreSQL"),
        status=kwargs.get("status", "new"),
        country=kwargs.get("country"),
        total_score=kwargs.get("total_score"),
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()
    return job_id


_add_job._counter = iter(range(1000))


def test_module_does_not_import_click_at_service_level():
    import jobsearch_core.run as run_module

    source = open(run_module.__file__, encoding="utf-8").read()
    assert "import click" not in source.split("def print_effective_defaults")[0], (
        "run.py service code must not depend on click (Phase 4.2a); "
        "only the print_* renderers may."
    )


def test_list_jobs_filters_without_click(db_url):
    """list_jobs service works headless and applies the documented filters."""
    _add_job(db_url, country="fi")
    _add_job(db_url, raw_title="Data Engineer", country="se")

    with patch("jobsearch_core.run.Settings") as mock_settings:
        mock_settings.return_value.database_url = db_url
        jobs = list_jobs(country="fi")
        assert [j.raw_company for j in jobs] == ["Example Oy"]

        all_jobs = list_jobs()
        assert len(all_jobs) == 2

        limited = list_jobs(limit=1)
        assert len(limited) == 1


def test_list_jobs_tailored_filter_with_session_open(db_url):
    """The tailored filter reads the lazy applications relationship before the
    session closes (would raise DetachedInstanceError otherwise)."""
    job_id = _add_job(db_url)
    from jobsearch_core.db.models import Application
    from jobsearch_core.db.session import get_session

    session = get_session()
    session.add(Application(job_id=job_id, base_variant="backend", status="draft"))
    session.commit()
    session.close()

    with patch("jobsearch_core.run.Settings") as mock_settings:
        mock_settings.return_value.database_url = db_url
        tailored = list_jobs(tailored=True)
        assert len(tailored) == 1
        not_tailored = list_jobs(tailored=False)
        assert not_tailored == []


@pytest.mark.parametrize("func", ["run_workflow", "collect_workflow", "match_jobs"])
def test_workflow_functions_accept_echo_callback(func):
    """All workflow entry points expose the optional echo parameter."""
    import inspect

    from jobsearch_core import run

    sig = inspect.signature(getattr(run, func))
    assert "echo" in sig.parameters
    assert sig.parameters["echo"].default is None


def test_print_run_summary_is_cli_layer_rendering(capsys):
    """print_run_summary renders without needing an echo callback on the
    summary object (unchanged CLI behavior)."""
    from jobsearch_core.run import print_run_summary

    summary = RunSummary(dry_run=True, mode="discover")
    print_run_summary(summary)
    out = capsys.readouterr().out
    assert "RUN SUMMARY" in out
