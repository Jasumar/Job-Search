"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from jobsearch_worker.consumer import RunWorker

from jobsearch_api.database import configure_database

from .routes import router


def create_app(database_url: str | None = None, *, start_worker: bool = False) -> FastAPI:
    """Create a configured local-only API application.

    The API is read/enqueue-only by default (Phase M1): pipeline execution is
    owned by the separate ``jobsearch-worker`` service. Passing
    ``start_worker=True`` opts back into the 4.2c in-process worker for tests
    or single-process deployments.
    """
    configure_database(database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = RunWorker() if start_worker else None
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(title="Job Search API", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
