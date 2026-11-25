"""Database lifecycle helpers for the API process."""

from __future__ import annotations

import os
from collections.abc import Generator

from jobsearch_core.config import Settings
from jobsearch_core.db.session import get_session, init_db
from sqlalchemy.orm import Session


def configure_database(database_url: str | None = None) -> str:
    """Initialize the core database and return the URL in use."""
    if database_url is not None:
        # ``list_jobs`` resolves its URL through Settings, so keep the API's
        # explicit URL aligned with that service function.
        os.environ["DATABASE_URL"] = database_url
    url = database_url or Settings().database_url
    init_db(url)
    return url


def get_db() -> Generator[Session, None, None]:
    """Provide a request-scoped session for API dependencies and tests."""
    session = get_session()
    try:
        yield session
    finally:
        session.close()
