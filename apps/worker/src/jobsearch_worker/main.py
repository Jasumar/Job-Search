"""Worker service entry point (Phase M1).

Long-running process that owns all pipeline execution: consumes ``job_runs``
rows enqueued by the API (or anything else) and executes them against core.
Run from the repo root so ``config/`` and ``data/`` resolve relative to CWD.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from jobsearch_core.config import Settings
from jobsearch_core.db.models import JobRun
from jobsearch_core.db.session import get_session, init_db

from jobsearch_worker.consumer import RunWorker, finish_run

logger = logging.getLogger("jobsearch_worker")


def _recover_stale_running_runs() -> int:
    """Fail any row left in ``running`` by a previous process.

    A crash mid-run would otherwise leave a stuck ``running`` row that
    permanently blocks the one-active-run-per-type rule. This runs once at
    startup, before the worker begins claiming, so it can never race a live
    run in a healthy single-worker deployment.
    """
    session = get_session()
    try:
        stale = session.query(JobRun).filter(JobRun.status == "running").all()
        for run in stale:
            logger.warning("Recovering stale running run %s (%s) as failed", run.id, run.run_type)
            finish_run(
                session,
                run,
                status="failed",
                error="worker process restarted while this run was in progress",
            )
        return len(stale)
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    url = Settings().database_url
    init_db(url)
    logger.info("Worker starting against %s", url)

    recovered = _recover_stale_running_runs()
    if recovered:
        logger.info("Recovered %d stale running run(s)", recovered)

    worker = RunWorker()
    worker.start()

    stop = threading.Event()

    def _handle(signum, _frame):
        logger.info("Received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        while not stop.is_set():
            stop.wait(timeout=3600)
    finally:
        worker.stop()
        logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
