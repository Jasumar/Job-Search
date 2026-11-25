from __future__ import annotations

import pytest
from jobsearch_core.cli import main
from jobsearch_core.config import Settings
from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.search.discover import clear_discovery_queue


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'jobs.db'}"


def _add_discovery(session, name, status):
    session.add(Company(name=name, status=status))
    session.commit()


def _statuses(session):
    return [d.status for d in session.query(Company).order_by(Company.id).all()]


def test_clear_pending_only_by_default(db_url):
    init_db(db_url)
    session = get_session()
    _add_discovery(session, "PendingCo", "pending")
    _add_discovery(session, "AcceptedCo", "accepted")
    _add_discovery(session, "RejectedCo", "rejected")
    session.close()

    count = clear_discovery_queue("pending")

    session = get_session()
    assert count == 1
    assert _statuses(session) == ["accepted", "rejected"]
    session.close()


def test_clear_by_status_scopes(db_url):
    init_db(db_url)
    session = get_session()
    for name, status in [
        ("A", "pending"),
        ("B", "accepted"),
        ("C", "rejected"),
    ]:
        _add_discovery(session, name, status)
    session.close()

    assert clear_discovery_queue("accepted") == 1
    assert clear_discovery_queue("rejected") == 1

    session = get_session()
    assert _statuses(session) == ["pending"]
    session.close()


def test_clear_all_removes_removable_but_not_known(db_url):
    init_db(db_url)
    session = get_session()
    _add_discovery(session, "A", "pending")
    _add_discovery(session, "B", "accepted")
    _add_discovery(session, "C", "rejected")
    _add_discovery(session, "KnownCo", "known")
    session.close()

    assert clear_discovery_queue("all") == 3

    session = get_session()
    assert _statuses(session) == ["known"]
    session.close()


def test_clear_known_is_rejected(db_url):
    init_db(db_url)
    with pytest.raises(ValueError):
        clear_discovery_queue("known")


def test_clear_cli_default_pending(db_url, runner, monkeypatch):
    init_db(db_url)
    session = get_session()
    _add_discovery(session, "PendingCo", "pending")
    _add_discovery(session, "AcceptedCo", "accepted")
    session.close()

    monkeypatch.setattr(
        "jobsearch_core.cli.Settings",
        lambda: Settings(database_url=db_url),
    )
    result = runner.invoke(main, ["discover", "clear"])

    assert result.exit_code == 0, result.output
    assert "Removed 1 company record(s) with status 'pending'." in result.output
    assert "known companies are protected" in result.output

    session = get_session()
    assert _statuses(session) == ["accepted"]
    session.close()


def test_clear_cli_all_flag(tmp_path, runner, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'all.db'}"
    init_db(db_url)
    session = get_session()
    _add_discovery(session, "A", "pending")
    _add_discovery(session, "B", "accepted")
    _add_discovery(session, "K", "known")
    session.close()

    monkeypatch.setattr(
        "jobsearch_core.cli.Settings",
        lambda: Settings(database_url=db_url),
    )
    result = runner.invoke(main, ["discover", "clear", "--status", "all"])

    assert result.exit_code == 0, result.output
    assert "Removed 2 company record(s) with status all." in result.output

    session = get_session()
    assert _statuses(session) == ["known"]
    session.close()
