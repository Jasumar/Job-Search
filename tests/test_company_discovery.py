from __future__ import annotations

import pytest
from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.search.discover import add_company, count_companies, get_company, list_companies


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'jobs.db'}"


def _seed(session, name, status="pending", country=None, careers_url=None):
    session.add(Company(name=name, status=status, country=country, careers_url=careers_url))
    session.commit()


def test_add_company_creates_durable_known_row(db_url):
    init_db(db_url)
    company = add_company(
        "Anyone AI",
        "https://anyone-ai.example/careers",
        notes="Careers page on employer domain.",
        country="FI",
    )

    assert company.id
    assert company.status == "known"
    assert company.verification_status == "verified"
    assert company.verified_at is not None
    assert company.notes == "Careers page on employer domain."
    assert company.country == "FI"


def test_list_companies_filters_by_status_and_country(db_url):
    init_db(db_url)
    session = get_session()
    _seed(session, "A", status="pending", country="FI")
    _seed(session, "B", status="accepted", country="FI")
    _seed(session, "C", status="known", country="EE")
    session.close()

    assert [c.name for c in list_companies(status="accepted")] == ["B"]
    assert {c.name for c in list_companies(country="fi")} == {"A", "B"}
    assert [c.name for c in list_companies(status="known", limit=1)] == ["C"]


def test_get_company_returns_none_when_missing(db_url):
    init_db(db_url)
    assert get_company(999) is None


def test_get_company_returns_record(db_url):
    init_db(db_url)
    session = get_session()
    _seed(session, "Example", status="known")
    session.close()
    company = get_company(1)
    assert company is not None
    assert company.name == "Example"


def test_count_companies_groups_by_status_and_country(db_url):
    init_db(db_url)
    session = get_session()
    _seed(session, "A", status="pending", country="FI")
    _seed(session, "B", status="pending", country="EE")
    _seed(session, "C", status="known", country="FI")
    _seed(session, "D", status="rejected", country=None)
    session.close()

    counts = count_companies()
    assert counts["by_status"] == {"pending": 2, "known": 1, "rejected": 1}
    assert counts["by_country"] == {"FI": 2, "EE": 1, "unknown": 1}
